"""车端软件灰度发布控制服务。

核心保证：
- 安装命令幂等：command_id 由 (计划, 波次, 车辆, 纪元, 尝试, 类型) 确定性派生，
  重复下发返回同一条命令；回执按 receipt_id 去重。
- 纪元围栏：回滚 / 隔离会提升车辆纪元，旧纪元的命令与迟到回执不再推进车辆。
- 判定不可篡改：每次判定连同规则版本与输入指标落事件日志；规则换版
  只影响显式迁移后的新判定，已完成的判定永不重算（重启回放也不重算）。
- 崩溃恢复：一切状态变更先写事件日志再应用，重启后回放即可继续。
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from . import risk
from .contracts import (
    CommandKind,
    CommandStatus,
    Compatibility,
    HealthReport,
    IncidentReport,
    InstallReceipt,
    PlanState,
    ReceiptDisposition,
    RiskPolicy,
    RolloutState,
    Severity,
    SoftwarePackage,
    VehicleSnapshot,
    VehicleStatus,
    Verdict,
)
from .state import (
    Assignment,
    CommandRecord,
    DecisionRecord,
    IncidentRecord,
    PlanRecord,
    QuarantineRecord,
    ReceiptRecord,
    Transition,
    VehicleRecord,
    WaveRecord,
)
from .store import EventStore

TERMINAL_WAVE_STATES = (RolloutState.COMPLETED, RolloutState.ROLLED_BACK)
TERMINAL_PLAN_STATES = (PlanState.COMPLETED, PlanState.ROLLED_BACK)
ACTIVE_ASSIGNMENT_STATUSES = (
    VehicleStatus.SCHEDULED,
    VehicleStatus.COMMAND_SENT,
    VehicleStatus.FAILED,
)
FINAL_ASSIGNMENT_STATUSES = (
    VehicleStatus.INSTALLED,
    VehicleStatus.FAILED,
    VehicleStatus.QUARANTINED,
    VehicleStatus.EXCLUDED,
    VehicleStatus.ROLLED_BACK,
)


class ServiceError(Exception):
    """服务层错误基类。status 供 API 层映射 HTTP 状态码。"""

    status = 400


class NotFoundError(ServiceError):
    status = 404


class ConflictError(ServiceError):
    status = 409


class ValidationError(ServiceError):
    status = 422


def _command_id(plan_id: str, wave_id: str, vehicle_id: str, epoch: int,
                kind: CommandKind, attempt: int) -> str:
    prefix = "i" if kind == CommandKind.INSTALL else "r"
    return f"cmd:{plan_id}:{wave_id}:{vehicle_id}:e{epoch}:{prefix}{attempt}"


class RolloutControlService:
    def __init__(self, data_dir: str | Path, clock: Callable[[], float] | None = None,
                 fsync: bool = True) -> None:
        self._clock = clock or time.time
        self._store = EventStore(data_dir, fsync=fsync)
        self._lock = threading.RLock()
        self._packages: dict[str, SoftwarePackage] = {}
        self._policies: dict[str, RiskPolicy] = {}
        self._vehicles: dict[str, VehicleRecord] = {}
        self._plans: dict[str, PlanRecord] = {}
        self._commands: dict[str, CommandRecord] = {}
        self._receipts: dict[str, ReceiptRecord] = {}
        self._health: dict[str, HealthReport] = {}
        self._incidents: dict[str, IncidentRecord] = {}
        self._quarantines: dict[str, QuarantineRecord] = {}
        self._events: list[dict] = []
        self._store.load_into(self._apply)

    # ------------------------------------------------------------------
    # 事件发射与应用
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, **payload) -> dict:
        event = self._store.append({"type": event_type, "ts": self._clock(), **payload})
        self._apply(event)
        return event

    def _apply(self, event: dict) -> None:
        etype = event["type"]
        data = event
        handler = getattr(self, f"_on_{etype}", None)
        if handler is None:
            raise ServiceError(f"未知事件类型: {etype}")
        handler(data)
        self._events.append(event)

    # ------------------------------------------------------------------
    # 登记：软件包 / 风险规则 / 车辆快照
    # ------------------------------------------------------------------

    def register_package(self, package: SoftwarePackage) -> SoftwarePackage:
        with self._lock:
            existing = self._packages.get(package.package_id)
            if existing is not None:
                if existing != package:
                    raise ConflictError(f"软件包 {package.package_id} 已存在且内容不同")
                return existing
            self._emit("package_registered", package=self._package_dict(package))
            return package

    def register_policy(self, policy: RiskPolicy) -> RiskPolicy:
        """登记风险规则版本。同版本号不允许静默覆盖。"""
        with self._lock:
            existing = self._policies.get(policy.version)
            if existing is not None:
                if existing != policy:
                    raise ConflictError(
                        f"规则版本 {policy.version} 已存在；请使用新版本号登记")
                return existing
            self._emit("policy_registered", policy=self._policy_dict(policy))
            return policy

    def register_vehicle(self, snapshot: VehicleSnapshot) -> VehicleSnapshot:
        """登记或刷新车辆快照（在线状态、健康回报不同步时可随时补登）。"""
        with self._lock:
            self._emit("vehicle_registered", vehicle=self._vehicle_dict(snapshot))
            return snapshot

    # ------------------------------------------------------------------
    # 计划与波次
    # ------------------------------------------------------------------

    def create_plan(self, package_id: str, policy_version: str,
                    wave_sizes: Iterable[float | int], created_by: str,
                    auto_promote: bool = True, plan_id: str | None = None) -> str:
        """按兼容条件筛选车辆并划分波次，入组理由随计划落库。"""
        with self._lock:
            package = self._packages.get(package_id)
            if package is None:
                raise NotFoundError(f"软件包不存在: {package_id}")
            if policy_version not in self._policies:
                raise NotFoundError(f"风险规则版本不存在: {policy_version}")
            sizes = self._normalize_wave_sizes(list(wave_sizes))

            plan_id = plan_id or f"plan-{len(self._plans) + 1}"
            if plan_id in self._plans:
                raise ConflictError(f"计划已存在: {plan_id}")

            eligible: list[tuple[VehicleSnapshot, list[str]]] = []
            exclusions: dict[str, list[str]] = {}
            for vid in sorted(self._vehicles):
                vehicle = self._vehicles[vid].snapshot
                reasons = self._eligibility(vehicle, package)
                if all(ok for ok, _ in reasons):
                    eligible.append((vehicle, [text for _, text in reasons]))
                else:
                    exclusions[vid] = [f"不满足: {text}"
                                       for ok, text in reasons if not ok]
            if not eligible:
                raise ValidationError("没有符合兼容条件的车辆，无法创建计划")

            # 波次规模语义：小数表示“剩余车辆的占比”，整数表示“辆数”；
            # 划分后剩余的车辆进入隐式末波次，保证最终覆盖全量。
            waves_payload = []
            cursor = 0
            total = len(eligible)
            ordinal = 0
            for size in sizes:
                remaining = total - cursor
                if remaining <= 0:
                    break
                if isinstance(size, float):
                    count = remaining if size >= 1.0 else math.floor(remaining * size)
                else:
                    count = min(size, remaining)
                if count <= 0:
                    continue
                ordinal += 1
                waves_payload.append(self._wave_payload(
                    plan_id, ordinal, eligible[cursor:cursor + count]))
                cursor += count
            if cursor < total:
                ordinal += 1
                waves_payload.append(self._wave_payload(
                    plan_id, ordinal, eligible[cursor:]))
            if not waves_payload:
                raise ValidationError("波次划分后没有车辆入组")

            self._emit(
                "plan_created",
                plan={
                    "plan_id": plan_id,
                    "package_id": package_id,
                    "policy_version": policy_version,
                    "created_by": created_by,
                    "auto_promote": auto_promote,
                    "created_at": self._clock(),
                },
                waves=waves_payload,
                exclusions=[{"vehicle_id": vid, "reasons": reasons}
                            for vid, reasons in sorted(exclusions.items())],
            )
            return plan_id

    def approve_plan(self, plan_id: str, approver: str) -> None:
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.state != PlanState.DRAFT:
                raise ConflictError(f"计划 {plan_id} 当前状态 {plan.state}，不能审批")
            self._emit("plan_approved", plan_id=plan_id, approver=approver)

    def start_next_wave(self, plan_id: str) -> dict:
        """启动下一个待发布波次。重复调用是安全的：波次已在运行则直接返回现状。"""
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.state in TERMINAL_PLAN_STATES:
                raise ConflictError(f"计划 {plan_id} 已终结（{plan.state}），不能继续放量")
            if plan.state == PlanState.PAUSED:
                raise ConflictError(f"计划 {plan_id} 已暂停，请先恢复")
            if plan.state == PlanState.DRAFT:
                raise ConflictError(f"计划 {plan_id} 尚未审批")
            active = plan.active_wave()
            if active is not None:
                return self._wave_brief(plan, active)
            wave = plan.next_pending_wave()
            if wave is None:
                raise ConflictError(f"计划 {plan_id} 没有待发布的波次")
            self._start_wave(plan, wave, reason="operator_start")
            return self._wave_brief(plan, wave)

    # ------------------------------------------------------------------
    # 回执 / 健康 / 事件上报
    # ------------------------------------------------------------------

    def ingest_receipt(self, receipt: InstallReceipt) -> ReceiptRecord:
        """受理安装/回滚回执。任何回执都会落库；重复与迟到回执不改变状态。"""
        with self._lock:
            existing = self._receipts.get(receipt.receipt_id)
            if existing is not None:
                # 幂等：同一 receipt_id 返回首次受理结果，并记录重复上报次数
                self._emit("receipt_duplicated", receipt_id=receipt.receipt_id)
                return existing

            command = self._commands.get(receipt.command_id)
            disposition = self._classify_receipt(receipt, command)
            payload = {
                "receipt": {
                    "receipt_id": receipt.receipt_id,
                    "command_id": receipt.command_id,
                    "vehicle_id": receipt.vehicle_id,
                    "success": receipt.success,
                    "error_code": receipt.error_code,
                    "occurred_at": receipt.occurred_at,
                },
                "disposition": disposition.value,
            }
            self._emit("receipt_recorded", **payload)
            record = self._receipts[receipt.receipt_id]

            if disposition == ReceiptDisposition.APPLIED and command is not None \
                    and command.kind == CommandKind.INSTALL:
                plan = self._plans.get(command.plan_id)
                if plan is not None and plan.state in (PlanState.RUNNING, PlanState.PAUSED):
                    self._evaluate(plan)
            return record

    def ingest_health(self, report: HealthReport) -> None:
        with self._lock:
            current = self._health.get(report.vehicle_id)
            if current is not None and current.reported_at > report.reported_at:
                return  # 迟到的旧健康回报，忽略但不算错误
            self._emit("health_recorded", report={
                "vehicle_id": report.vehicle_id,
                "health_score": report.health_score,
                "reported_at": report.reported_at,
                "fault_codes": list(report.fault_codes),
            })
            self._evaluate_all_active()

    def file_incident(self, incident: IncidentReport) -> None:
        with self._lock:
            if incident.incident_id in self._incidents:
                raise ConflictError(f"事件已存在: {incident.incident_id}")
            self._emit("incident_recorded", incident={
                "incident_id": incident.incident_id,
                "severity": incident.severity.value,
                "summary": incident.summary,
                "vehicle_id": incident.vehicle_id,
                "hardware_batch": incident.hardware_batch,
                "reported_at": incident.reported_at or self._clock(),
            })
            self._evaluate_all_active()

    def resolve_incident(self, incident_id: str) -> None:
        with self._lock:
            if incident_id not in self._incidents:
                raise NotFoundError(f"事件不存在: {incident_id}")
            if self._incidents[incident_id].resolved:
                return
            self._emit("incident_resolved", incident_id=incident_id)

    def retry_vehicle(self, plan_id: str, wave_id: str, vehicle_id: str) -> CommandRecord:
        """对失败的车辆重发安装命令（新尝试序号，旧命令被取代）。"""
        with self._lock:
            plan = self._require_plan(plan_id)
            wave = self._require_wave(plan, wave_id)
            if wave.state not in (RolloutState.RUNNING, RolloutState.PAUSED):
                raise ConflictError(f"波次 {wave_id} 状态 {wave.state}，不能重试")
            assignment = wave.cohort.get(vehicle_id)
            if assignment is None:
                raise NotFoundError(f"车辆 {vehicle_id} 不在波次 {wave_id}")
            if assignment.status != VehicleStatus.FAILED:
                raise ConflictError(f"车辆 {vehicle_id} 状态 {assignment.status}，无需重试")
            latest = self._latest_command(plan_id, wave_id, vehicle_id, CommandKind.INSTALL)
            assert latest is not None
            command = self._issue_command(
                plan, wave, vehicle_id, assignment.epoch,
                CommandKind.INSTALL, latest.attempt + 1)
            self._emit(
                "command_retry_issued",
                plan_id=plan_id, wave_id=wave_id,
                superseded_command_id=latest.command_id,
                command=command.to_dict(),
            )
            return self._commands[command.command_id]

    # ------------------------------------------------------------------
    # 风险判定与人工动作
    # ------------------------------------------------------------------

    def evaluate_plan(self, plan_id: str) -> DecisionRecord | None:
        """显式触发一次风险判定（始终落库）。"""
        with self._lock:
            plan = self._require_plan(plan_id)
            return self._evaluate(plan, always_record=True)

    def resume_plan(self, plan_id: str, operator: str) -> None:
        """恢复暂停的计划。阻塞性恢复条件未满足时拒绝。"""
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.state != PlanState.PAUSED:
                raise ConflictError(f"计划 {plan_id} 状态 {plan.state}，不在暂停中")
            wave = plan.active_wave()
            if wave is None or wave.state != RolloutState.PAUSED:
                raise ConflictError(f"计划 {plan_id} 没有可恢复的波次")
            blockers = [c for c in self._recovery_conditions(plan, wave)
                        if c["blocking"] and not c["met"] and c["id"] != "manual_resume"]
            if blockers:
                raise ConflictError(
                    "恢复条件未满足: " + "; ".join(c["detail"] for c in blockers))
            self._emit("wave_state_changed", plan_id=plan_id, wave_id=wave.wave_id,
                       from_state=wave.state.value, to_state=RolloutState.RUNNING.value,
                       reason=f"运维 {operator} 手动恢复", decision_id="")
            self._emit("plan_state_changed", plan_id=plan_id,
                       from_state=PlanState.PAUSED.value, to_state=PlanState.RUNNING.value,
                       reason=f"运维 {operator} 手动恢复", decision_id="")
            self._evaluate(plan)

    def rollback_plan(self, plan_id: str, reason: str, operator: str = "manual") -> None:
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.state in TERMINAL_PLAN_STATES:
                raise ConflictError(f"计划 {plan_id} 已终结，不能回滚")
            if plan.state == PlanState.DRAFT:
                raise ConflictError(f"计划 {plan_id} 尚未开始，无需回滚")
            self._execute_rollback(plan, reason=f"{reason}（操作人: {operator}）",
                                   decision_id="")

    def migrate_plan_policy(self, plan_id: str, new_version: str, reason: str) -> None:
        """将计划迁移到新规则版本。只影响之后的判定，已完成的判定保持原样。"""
        with self._lock:
            plan = self._require_plan(plan_id)
            if plan.state in TERMINAL_PLAN_STATES:
                raise ConflictError(f"计划 {plan_id} 已终结，不能迁移规则")
            if new_version not in self._policies:
                raise NotFoundError(f"风险规则版本不存在: {new_version}")
            if plan.policy_version == new_version:
                raise ConflictError(f"计划已在使用规则版本 {new_version}")
            self._emit("plan_policy_migrated", plan_id=plan_id,
                       from_version=plan.policy_version, to_version=new_version,
                       reason=reason)

    def clear_quarantine(self, hardware_batch: str, operator: str) -> None:
        """解除批次隔离。已隔离车辆的历史状态不变，只影响后续新计划。"""
        with self._lock:
            record = self._quarantines.get(hardware_batch)
            if record is None or record.cleared:
                raise NotFoundError(f"批次 {hardware_batch} 没有生效中的隔离")
            self._emit("quarantine_cleared", hardware_batch=hardware_batch,
                       operator=operator)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: str) -> dict:
        with self._lock:
            plan = self._require_plan(plan_id)
            return {
                "plan_id": plan.plan_id,
                "package_id": plan.package_id,
                "policy_version": plan.policy_version,
                "state": plan.state.value,
                "created_by": plan.created_by,
                "approved_by": plan.approved_by,
                "auto_promote": plan.auto_promote,
                "waves": [self._wave_brief(plan, w) for w in plan.waves],
                "exclusions": [
                    {"vehicle_id": vid, "reasons": reasons}
                    for vid, reasons in sorted(plan.exclusions.items())
                ],
                "transitions": [t.to_dict() for t in plan.transitions],
            }

    def wave_report(self, plan_id: str, wave_id: str) -> dict:
        """运维视图：入组理由、实时风险预算、状态迁移、恢复条件。"""
        with self._lock:
            plan = self._require_plan(plan_id)
            wave = self._require_wave(plan, wave_id)
            policy = self._policies[plan.policy_version]
            metrics = self._compute_metrics(plan, wave)
            return {
                "plan_id": plan_id,
                "wave_id": wave_id,
                "ordinal": wave.ordinal,
                "state": wave.state.value,
                "policy_version": plan.policy_version,
                "cohort": [
                    {
                        "vehicle_id": a.vehicle_id,
                        "status": a.status.value,
                        "epoch": a.epoch,
                        "attempts": a.attempts,
                        "reasons": list(a.reasons),
                    }
                    for a in sorted(wave.cohort.values(),
                                    key=lambda a: a.vehicle_id)
                ],
                "metrics": metrics.to_dict(),
                "risk_budget": self._risk_budget(metrics, policy),
                "transitions": [t.to_dict() for t in wave.transitions],
                "decisions": [d.to_dict() for d in plan.decisions
                              if d.wave_id == wave_id],
                "recovery_conditions": self._recovery_conditions(plan, wave),
                "quarantined_batches": sorted(
                    batch for batch, rec in self._quarantines.items()
                    if not rec.cleared),
            }

    def list_decisions(self, plan_id: str) -> list[dict]:
        with self._lock:
            plan = self._require_plan(plan_id)
            return [d.to_dict() for d in plan.decisions]

    def get_vehicle(self, vehicle_id: str) -> dict:
        with self._lock:
            record = self._vehicles.get(vehicle_id)
            if record is None:
                raise NotFoundError(f"车辆不存在: {vehicle_id}")
            assignments = []
            for plan in self._plans.values():
                for wave in plan.waves:
                    assignment = wave.cohort.get(vehicle_id)
                    if assignment is not None:
                        assignments.append({
                            "plan_id": plan.plan_id,
                            "wave_id": wave.wave_id,
                            "status": assignment.status.value,
                            "epoch": assignment.epoch,
                        })
            health = self._health.get(vehicle_id)
            return {
                "vehicle": self._vehicle_dict(record.snapshot),
                "epoch": record.epoch,
                "latest_health": None if health is None else {
                    "health_score": health.health_score,
                    "reported_at": health.reported_at,
                    "fault_codes": list(health.fault_codes),
                },
                "assignments": assignments,
            }

    def audit_log(self, plan_id: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            events = self._events
            if plan_id is not None:
                events = [e for e in events
                          if e.get("plan_id") == plan_id
                          or e.get("plan", {}).get("plan_id") == plan_id]
            return list(events[-limit:])

    # ------------------------------------------------------------------
    # 内部：资格筛选与波次构建
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_wave_sizes(sizes: list[float | int]) -> list[float | int]:
        if not sizes:
            raise ValidationError("至少需要一个波次")
        is_fraction = all(isinstance(s, float) and 0.0 < s <= 1.0 for s in sizes)
        is_count = all(isinstance(s, int) and not isinstance(s, bool) and s >= 1
                       for s in sizes)
        if not (is_fraction or is_count):
            raise ValidationError(
                "波次规模必须全为 (0,1] 的比例（占剩余车辆）或全为正整数辆数")
        return list(sizes)

    def _eligibility(self, vehicle: VehicleSnapshot,
                     package: SoftwarePackage) -> list[tuple[bool, str]]:
        compat: Compatibility = package.compatibility
        checks: list[tuple[bool, str]] = []
        quarantine = self._quarantines.get(vehicle.hardware_batch)
        checks.append((quarantine is None or quarantine.cleared,
                       f"硬件批次 {vehicle.hardware_batch} 未处于隔离中"))
        if compat.hardware_batches:
            checks.append((vehicle.hardware_batch in compat.hardware_batches,
                           f"硬件批次 {vehicle.hardware_batch} 在兼容列表"))
        if compat.models:
            checks.append((vehicle.model in compat.models,
                           f"车型 {vehicle.model} 在兼容列表"))
        if compat.source_versions:
            checks.append((vehicle.software_version in compat.source_versions,
                           f"源版本 {vehicle.software_version} 允许升级"))
        checks.append((vehicle.battery_percent >= compat.min_battery_percent,
                       f"电量 {vehicle.battery_percent} ≥ 最低要求 "
                       f"{compat.min_battery_percent}"))
        if compat.require_online:
            checks.append((vehicle.online, "车辆在线"))
        checks.append((vehicle.software_version != package.version,
                       f"当前版本不是目标版本 {package.version}"))
        return checks

    def _wave_payload(self, plan_id: str, ordinal: int,
                      vehicles: list[tuple[VehicleSnapshot, list[str]]]) -> dict:
        wave_id = f"{plan_id}-w{ordinal}"
        return {
            "wave_id": wave_id,
            "ordinal": ordinal,
            "assignments": [
                {
                    "vehicle_id": vehicle.vehicle_id,
                    "epoch": self._vehicles[vehicle.vehicle_id].epoch,
                    "reasons": reasons,
                }
                for vehicle, reasons in vehicles
            ],
        }

    def _start_wave(self, plan: PlanRecord, wave: WaveRecord, reason: str,
                    decision_id: str = "") -> None:
        if plan.state == PlanState.APPROVED:
            self._emit("plan_state_changed", plan_id=plan.plan_id,
                       from_state=PlanState.APPROVED.value,
                       to_state=PlanState.RUNNING.value,
                       reason=reason, decision_id=decision_id)
        commands = []
        excluded = []
        for assignment in wave.cohort.values():
            if assignment.status != VehicleStatus.SCHEDULED:
                continue
            vehicle = self._vehicles[assignment.vehicle_id]
            quarantine = self._quarantines.get(vehicle.snapshot.hardware_batch)
            if vehicle.epoch != assignment.epoch:
                excluded.append({"vehicle_id": assignment.vehicle_id,
                                 "reason": "车辆纪元已变化（曾被回滚或隔离），围栏排除"})
            elif quarantine is not None and not quarantine.cleared:
                excluded.append({"vehicle_id": assignment.vehicle_id,
                                 "reason": f"硬件批次 {vehicle.snapshot.hardware_batch} 处于隔离中"})
            else:
                command = self._issue_command(
                    plan, wave, assignment.vehicle_id, assignment.epoch,
                    CommandKind.INSTALL, 1)
                commands.append(command.to_dict())
        self._emit("wave_started", plan_id=plan.plan_id, wave_id=wave.wave_id,
                   reason=reason, decision_id=decision_id,
                   commands=commands, excluded=excluded)

    def _issue_command(self, plan: PlanRecord, wave: WaveRecord, vehicle_id: str,
                       epoch: int, kind: CommandKind, attempt: int) -> CommandRecord:
        command_id = _command_id(plan.plan_id, wave.wave_id, vehicle_id,
                                 epoch, kind, attempt)
        existing = self._commands.get(command_id)
        if existing is not None:
            return existing  # 幂等：同一命令重复下发返回原记录
        return CommandRecord(
            command_id=command_id,
            plan_id=plan.plan_id,
            wave_id=wave.wave_id,
            vehicle_id=vehicle_id,
            epoch=epoch,
            attempt=attempt,
            kind=kind,
            created_at=self._clock(),
        )

    # ------------------------------------------------------------------
    # 内部：回执分类
    # ------------------------------------------------------------------

    def _classify_receipt(self, receipt: InstallReceipt,
                          command: CommandRecord | None) -> ReceiptDisposition:
        if command is None or command.vehicle_id != receipt.vehicle_id:
            return ReceiptDisposition.UNKNOWN_COMMAND
        vehicle = self._vehicles.get(receipt.vehicle_id)
        if vehicle is None or vehicle.epoch != command.epoch:
            return ReceiptDisposition.STALE_EPOCH
        if command.status == CommandStatus.SUPERSEDED:
            return ReceiptDisposition.SUPERSEDED_COMMAND
        if command.status in (CommandStatus.SUCCEEDED, CommandStatus.FAILED):
            return ReceiptDisposition.DUPLICATE_COMMAND
        plan = self._plans.get(command.plan_id)
        wave = self._find_wave(plan, command.wave_id) if plan else None
        # 安装命令在波次终结后不再受理；回滚命令恰恰在波次关闭时下发，不受此限。
        if command.kind == CommandKind.INSTALL \
                and wave is not None and wave.state in TERMINAL_WAVE_STATES:
            return ReceiptDisposition.LATE_WAVE_CLOSED
        return ReceiptDisposition.APPLIED

    # ------------------------------------------------------------------
    # 内部：风险评定
    # ------------------------------------------------------------------

    def _evaluate_all_active(self) -> None:
        for plan in self._plans.values():
            if plan.state in (PlanState.RUNNING, PlanState.PAUSED) \
                    and plan.active_wave() is not None:
                self._evaluate(plan)

    def _evaluate(self, plan: PlanRecord, always_record: bool = False) -> DecisionRecord | None:
        wave = plan.active_wave()
        if wave is None or plan.state in TERMINAL_PLAN_STATES:
            return None
        policy = self._policies[plan.policy_version]

        while True:
            metrics = self._compute_metrics(plan, wave)
            result = risk.evaluate(metrics, policy)
            if result.verdict != Verdict.QUARANTINE_BATCH:
                break
            decision = self._record_decision(plan, wave, result, metrics)
            for batch in result.anomalous_batches:
                self._quarantine_batch(plan, batch, decision)

        decision = None
        wave_completed = result.verdict in (Verdict.CONTINUE, Verdict.INSUFFICIENT_DATA) \
            and self._wave_complete(wave)
        last = plan.decisions[-1] if plan.decisions else None
        notable = (
            always_record
            or result.verdict not in (Verdict.CONTINUE, Verdict.INSUFFICIENT_DATA)
            or last is None
            or last.verdict != result.verdict.value
            or wave_completed
        )
        if notable:
            decision = self._record_decision(plan, wave, result, metrics)
            self._apply_verdict(plan, wave, result.verdict, decision)
        return decision

    def _record_decision(self, plan: PlanRecord, wave: WaveRecord,
                         result: risk.RiskVerdict,
                         metrics: risk.WaveMetrics) -> DecisionRecord:
        decision_id = f"dec:{plan.plan_id}:{len(plan.decisions) + 1}"
        self._emit("decision_made", decision={
            "decision_id": decision_id,
            "plan_id": plan.plan_id,
            "wave_id": wave.wave_id,
            "policy_version": plan.policy_version,
            "verdict": result.verdict.value,
            "metrics": metrics.to_dict(),
            "reasons": list(result.reasons),
            "created_at": self._clock(),
        })
        return plan.decisions[-1]

    def _apply_verdict(self, plan: PlanRecord, wave: WaveRecord,
                       verdict: Verdict, decision: DecisionRecord) -> None:
        if verdict == Verdict.ROLLBACK:
            self._execute_rollback(plan, reason="风险判定触发自动回滚",
                                   decision_id=decision.decision_id)
        elif verdict == Verdict.PAUSE:
            if wave.state == RolloutState.RUNNING:
                self._emit("wave_state_changed", plan_id=plan.plan_id,
                           wave_id=wave.wave_id,
                           from_state=wave.state.value,
                           to_state=RolloutState.PAUSED.value,
                           reason="风险判定触发暂停",
                           decision_id=decision.decision_id)
            if plan.state == PlanState.RUNNING:
                self._emit("plan_state_changed", plan_id=plan.plan_id,
                           from_state=PlanState.RUNNING.value,
                           to_state=PlanState.PAUSED.value,
                           reason="风险判定触发暂停",
                           decision_id=decision.decision_id)
        elif verdict in (Verdict.CONTINUE, Verdict.INSUFFICIENT_DATA):
            # 波次内车辆全部到达终态（含被隔离/排除）即可收官；
            # 数据不足但已无在途车辆时同样收尾，避免波次悬挂。
            if wave.state == RolloutState.RUNNING and self._wave_complete(wave):
                self._complete_wave(plan, wave, decision)

    def _wave_complete(self, wave: WaveRecord) -> bool:
        return all(a.status in FINAL_ASSIGNMENT_STATUSES
                   for a in wave.cohort.values())

    def _complete_wave(self, plan: PlanRecord, wave: WaveRecord,
                       decision: DecisionRecord) -> None:
        self._emit("wave_state_changed", plan_id=plan.plan_id, wave_id=wave.wave_id,
                   from_state=wave.state.value, to_state=RolloutState.COMPLETED.value,
                   reason="波次全部车辆到达终态且风险在预算内",
                   decision_id=decision.decision_id)
        next_wave = plan.next_pending_wave()
        if next_wave is None:
            self._emit("plan_state_changed", plan_id=plan.plan_id,
                       from_state=plan.state.value,
                       to_state=PlanState.COMPLETED.value,
                       reason="全部波次完成",
                       decision_id=decision.decision_id)
        elif plan.auto_promote:
            self._start_wave(plan, next_wave, reason="auto_promote",
                             decision_id=decision.decision_id)

    def _execute_rollback(self, plan: PlanRecord, reason: str,
                          decision_id: str) -> None:
        rollback_commands = []
        superseded = []
        excluded = []
        epoch_bumps = []
        rolling_back = []
        for wave in plan.waves:
            for assignment in wave.cohort.values():
                vehicle = self._vehicles[assignment.vehicle_id]
                if assignment.status == VehicleStatus.INSTALLED:
                    command = self._issue_command(
                        plan, wave, assignment.vehicle_id, vehicle.epoch + 1,
                        CommandKind.ROLLBACK, 1)
                    rollback_commands.append(command.to_dict())
                    rolling_back.append(assignment.vehicle_id)
                    epoch_bumps.append({"vehicle_id": assignment.vehicle_id,
                                        "new_epoch": vehicle.epoch + 1})
                elif assignment.status in ACTIVE_ASSIGNMENT_STATUSES:
                    excluded.append({"vehicle_id": assignment.vehicle_id,
                                     "reason": "计划回滚，未安装车辆被排除"})
                    for command in self._commands_for(plan.plan_id, wave.wave_id,
                                                      assignment.vehicle_id):
                        if command.status == CommandStatus.SENT:
                            superseded.append(command.command_id)
        self._emit("rollback_started", plan_id=plan.plan_id, reason=reason,
                   decision_id=decision_id,
                   rollback_commands=rollback_commands,
                   superseded_commands=superseded, excluded=excluded,
                   epoch_bumps=epoch_bumps, rolling_back=rolling_back)
        for wave in plan.waves:
            if wave.state in (RolloutState.RUNNING, RolloutState.PAUSED,
                              RolloutState.PENDING):
                self._emit("wave_state_changed", plan_id=plan.plan_id,
                           wave_id=wave.wave_id, from_state=wave.state.value,
                           to_state=RolloutState.ROLLED_BACK.value,
                           reason=reason, decision_id=decision_id)
        self._emit("plan_state_changed", plan_id=plan.plan_id,
                   from_state=plan.state.value,
                   to_state=PlanState.ROLLED_BACK.value,
                   reason=reason, decision_id=decision_id)

    def _quarantine_batch(self, plan: PlanRecord, batch: str,
                          decision: DecisionRecord) -> None:
        quarantined = []
        superseded = []
        epoch_bumps = []
        for wave in plan.waves:
            for assignment in wave.cohort.values():
                vehicle = self._vehicles[assignment.vehicle_id]
                if vehicle.snapshot.hardware_batch != batch:
                    continue
                if assignment.status in ACTIVE_ASSIGNMENT_STATUSES:
                    quarantined.append(assignment.vehicle_id)
                    for command in self._commands_for(plan.plan_id, wave.wave_id,
                                                      assignment.vehicle_id):
                        if command.status == CommandStatus.SENT:
                            superseded.append(command.command_id)
                    epoch_bumps.append({"vehicle_id": assignment.vehicle_id,
                                        "new_epoch": vehicle.epoch + 1})
        self._emit("batch_quarantined", plan_id=plan.plan_id,
                   hardware_batch=batch, decision_id=decision.decision_id,
                   reason="；".join(decision.reasons),
                   quarantined_vehicles=quarantined,
                   superseded_commands=superseded, epoch_bumps=epoch_bumps)

    # ------------------------------------------------------------------
    # 内部：指标与预算
    # ------------------------------------------------------------------

    def _compute_metrics(self, plan: PlanRecord, wave: WaveRecord) -> risk.WaveMetrics:
        policy = self._policies[plan.policy_version]
        cohort = [a for a in wave.cohort.values()
                  if a.status not in (VehicleStatus.QUARANTINED,
                                      VehicleStatus.EXCLUDED)]
        dispatched = terminal = successes = failures = pending = unhealthy = 0
        batch_stats: dict[str, list[int]] = {}  # batch -> [terminal, failures]
        cohort_batches: set[str] = set()
        for assignment in cohort:
            vehicle = self._vehicles[assignment.vehicle_id]
            batch = vehicle.snapshot.hardware_batch
            cohort_batches.add(batch)
            stats = batch_stats.setdefault(batch, [0, 0])
            command = self._latest_command(plan.plan_id, wave.wave_id,
                                           assignment.vehicle_id,
                                           CommandKind.INSTALL)
            if command is not None and command.status != CommandStatus.SUPERSEDED:
                dispatched += 1
                if command.status == CommandStatus.SUCCEEDED:
                    terminal += 1
                    successes += 1
                    stats[0] += 1
                elif command.status == CommandStatus.FAILED:
                    terminal += 1
                    failures += 1
                    stats[0] += 1
                    stats[1] += 1
                else:
                    pending += 1
            report = self._health.get(assignment.vehicle_id)
            if report is not None and report.health_score < policy.unhealthy_score_below:
                unhealthy += 1
        cohort_ids = {a.vehicle_id for a in cohort}
        critical = sum(
            1 for inc in self._incidents.values()
            if not inc.resolved and inc.severity == Severity.CRITICAL.value
            and (inc.vehicle_id in cohort_ids or inc.hardware_batch in cohort_batches)
        )
        return risk.WaveMetrics(
            cohort_size=len(cohort),
            dispatched=dispatched,
            terminal=terminal,
            successes=successes,
            failures=failures,
            pending=pending,
            unhealthy=unhealthy,
            critical_incidents=critical,
            batches=tuple(
                risk.BatchMetrics(batch, stats[0], stats[1])
                for batch, stats in sorted(batch_stats.items())
            ),
        )

    @staticmethod
    def _risk_budget(metrics: risk.WaveMetrics, policy: RiskPolicy) -> dict:
        def trigger_units(rate: float) -> int:
            return math.ceil(rate * metrics.cohort_size - 1e-9) \
                if rate * metrics.cohort_size > 0 else 0

        pause_at = trigger_units(policy.pause_failure_rate)
        rollback_at = trigger_units(policy.rollback_failure_rate)
        consumed = metrics.risk_units
        return {
            "policy_version": policy.version,
            "cohort_size": metrics.cohort_size,
            "pause_threshold_rate": policy.pause_failure_rate,
            "rollback_threshold_rate": policy.rollback_failure_rate,
            "consumed_risk_units": consumed,
            "pause_at_units": pause_at,
            "rollback_at_units": rollback_at,
            "remaining_before_pause": max(0, pause_at - consumed),
            "remaining_before_rollback": max(0, rollback_at - consumed),
        }

    def _recovery_conditions(self, plan: PlanRecord, wave: WaveRecord) -> list[dict]:
        policy = self._policies[plan.policy_version]
        metrics = self._compute_metrics(plan, wave)
        anomalous_open = [
            b.hardware_batch for b in metrics.batches
            if b.terminal >= policy.min_batch_sample
            and b.failure_rate >= policy.batch_quarantine_rate
            and (b.hardware_batch not in self._quarantines
                 or self._quarantines[b.hardware_batch].cleared)
        ]
        return [
            {
                "id": "critical_incidents_clear",
                "met": metrics.critical_incidents == 0,
                "blocking": True,
                "detail": f"未解决严重事件 {metrics.critical_incidents} 起",
            },
            {
                "id": "anomalous_batches_quarantined",
                "met": not anomalous_open,
                "blocking": True,
                "detail": "异常批次均已隔离" if not anomalous_open
                          else f"待隔离批次: {', '.join(anomalous_open)}",
            },
            {
                "id": "risk_rate_below_pause_threshold",
                "met": metrics.risk_rate < policy.pause_failure_rate,
                "blocking": False,
                "detail": f"当前风险率 {metrics.risk_rate:.2%}，暂停阈值 "
                          f"{policy.pause_failure_rate:.2%}（建议项，不阻塞恢复）",
            },
            {
                "id": "manual_resume",
                "met": False,
                "blocking": True,
                "detail": "需运维人员确认并调用 resume",
            },
        ]

    # ------------------------------------------------------------------
    # 内部：查找辅助
    # ------------------------------------------------------------------

    def _require_plan(self, plan_id: str) -> PlanRecord:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"计划不存在: {plan_id}")
        return plan

    @staticmethod
    def _find_wave(plan: PlanRecord | None, wave_id: str) -> WaveRecord | None:
        if plan is None:
            return None
        for wave in plan.waves:
            if wave.wave_id == wave_id:
                return wave
        return None

    def _require_wave(self, plan: PlanRecord, wave_id: str) -> WaveRecord:
        wave = self._find_wave(plan, wave_id)
        if wave is None:
            raise NotFoundError(f"波次不存在: {wave_id}")
        return wave

    def _commands_for(self, plan_id: str, wave_id: str,
                      vehicle_id: str) -> list[CommandRecord]:
        return [c for c in self._commands.values()
                if c.plan_id == plan_id and c.wave_id == wave_id
                and c.vehicle_id == vehicle_id]

    def _latest_command(self, plan_id: str, wave_id: str, vehicle_id: str,
                        kind: CommandKind) -> CommandRecord | None:
        commands = [c for c in self._commands_for(plan_id, wave_id, vehicle_id)
                    if c.kind == kind]
        if not commands:
            return None
        return max(commands, key=lambda c: c.attempt)

    def _wave_brief(self, plan: PlanRecord, wave: WaveRecord) -> dict:
        commands = [c for c in self._commands.values() if c.wave_id == wave.wave_id]
        return {
            "wave_id": wave.wave_id,
            "ordinal": wave.ordinal,
            "state": wave.state.value,
            "cohort_size": len(wave.cohort),
            "commands": [c.to_dict() for c in sorted(
                commands, key=lambda c: c.command_id)],
        }

    # ------------------------------------------------------------------
    # 事件应用（回放路径，禁止再发射事件）
    # ------------------------------------------------------------------

    def _on_package_registered(self, data: dict) -> None:
        p = data["package"]
        compat = p["compatibility"]
        self._packages[p["package_id"]] = SoftwarePackage(
            package_id=p["package_id"],
            version=p["version"],
            compatibility=Compatibility(
                hardware_batches=frozenset(compat["hardware_batches"]),
                source_versions=frozenset(compat["source_versions"]),
                min_battery_percent=compat["min_battery_percent"],
                require_online=compat["require_online"],
                models=frozenset(compat["models"]),
            ),
            description=p.get("description", ""),
        )

    def _on_policy_registered(self, data: dict) -> None:
        p = data["policy"]
        self._policies[p["version"]] = RiskPolicy(**p)

    def _on_vehicle_registered(self, data: dict) -> None:
        v = data["vehicle"]
        snapshot = VehicleSnapshot(**v)
        record = self._vehicles.get(snapshot.vehicle_id)
        if record is None:
            self._vehicles[snapshot.vehicle_id] = VehicleRecord(snapshot=snapshot)
        else:
            record.snapshot = snapshot

    def _on_plan_created(self, data: dict) -> None:
        meta = data["plan"]
        plan = PlanRecord(
            plan_id=meta["plan_id"],
            package_id=meta["package_id"],
            policy_version=meta["policy_version"],
            created_by=meta["created_by"],
            auto_promote=meta["auto_promote"],
            created_at=meta.get("created_at", 0.0),
        )
        for wave_data in data["waves"]:
            wave = WaveRecord(wave_id=wave_data["wave_id"],
                              ordinal=wave_data["ordinal"])
            for a in wave_data["assignments"]:
                wave.cohort[a["vehicle_id"]] = Assignment(
                    vehicle_id=a["vehicle_id"],
                    epoch=a["epoch"],
                    reasons=list(a["reasons"]),
                )
            plan.waves.append(wave)
        plan.exclusions = {e["vehicle_id"]: list(e["reasons"])
                           for e in data.get("exclusions", [])}
        self._plans[plan.plan_id] = plan

    def _on_plan_approved(self, data: dict) -> None:
        plan = self._plans[data["plan_id"]]
        plan.state = PlanState.APPROVED
        plan.approved_by = data["approver"]
        plan.transitions.append(Transition(
            PlanState.DRAFT.value, PlanState.APPROVED.value,
            data["ts"], f"审批人: {data['approver']}"))

    def _on_wave_started(self, data: dict) -> None:
        plan = self._plans[data["plan_id"]]
        wave = self._find_wave(plan, data["wave_id"])
        assert wave is not None
        wave.transitions.append(Transition(
            wave.state.value, RolloutState.RUNNING.value,
            data["ts"], data.get("reason", ""), data.get("decision_id", "")))
        wave.state = RolloutState.RUNNING
        wave.started_at = data["ts"]
        for command_data in data["commands"]:
            command = CommandRecord.from_dict(command_data)
            self._commands[command.command_id] = command
            assignment = wave.cohort[command.vehicle_id]
            assignment.status = VehicleStatus.COMMAND_SENT
            assignment.attempts = command.attempt
        for item in data.get("excluded", []):
            assignment = wave.cohort[item["vehicle_id"]]
            assignment.status = VehicleStatus.EXCLUDED
            assignment.reasons.append(item["reason"])

    def _on_command_retry_issued(self, data: dict) -> None:
        old = self._commands[data["superseded_command_id"]]
        old.status = CommandStatus.SUPERSEDED
        command = CommandRecord.from_dict(data["command"])
        self._commands[command.command_id] = command
        plan = self._plans[data["plan_id"]]
        wave = self._find_wave(plan, data["wave_id"])
        assert wave is not None
        assignment = wave.cohort[command.vehicle_id]
        assignment.status = VehicleStatus.COMMAND_SENT
        assignment.attempts = command.attempt

    def _on_receipt_recorded(self, data: dict) -> None:
        r = data["receipt"]
        record = ReceiptRecord(
            receipt_id=r["receipt_id"],
            command_id=r["command_id"],
            vehicle_id=r["vehicle_id"],
            success=r["success"],
            disposition=data["disposition"],
            error_code=r.get("error_code", ""),
            occurred_at=r.get("occurred_at", 0.0),
            received_at=data["ts"],
        )
        self._receipts[record.receipt_id] = record
        if data["disposition"] != ReceiptDisposition.APPLIED.value:
            return
        command = self._commands[r["command_id"]]
        command.status = CommandStatus.SUCCEEDED if r["success"] else CommandStatus.FAILED
        command.error_code = r.get("error_code", "")
        command.closed_by_receipt = r["receipt_id"]
        plan = self._plans[command.plan_id]
        wave = self._find_wave(plan, command.wave_id)
        assert wave is not None
        assignment = wave.cohort[r["vehicle_id"]]
        if command.kind == CommandKind.INSTALL:
            assignment.status = (VehicleStatus.INSTALLED if r["success"]
                                 else VehicleStatus.FAILED)
        elif r["success"]:
            assignment.status = VehicleStatus.ROLLED_BACK

    def _on_receipt_duplicated(self, data: dict) -> None:
        self._receipts[data["receipt_id"]].duplicate_count += 1

    def _on_health_recorded(self, data: dict) -> None:
        r = data["report"]
        self._health[r["vehicle_id"]] = HealthReport(
            vehicle_id=r["vehicle_id"],
            health_score=r["health_score"],
            reported_at=r["reported_at"],
            fault_codes=tuple(r.get("fault_codes", ())),
        )

    def _on_incident_recorded(self, data: dict) -> None:
        inc = data["incident"]
        self._incidents[inc["incident_id"]] = IncidentRecord(**inc)

    def _on_incident_resolved(self, data: dict) -> None:
        incident = self._incidents[data["incident_id"]]
        incident.resolved = True
        incident.resolved_at = data["ts"]

    def _on_decision_made(self, data: dict) -> None:
        decision = DecisionRecord.from_dict(data["decision"])
        decision.seq = data["seq"]
        self._plans[decision.plan_id].decisions.append(decision)

    def _on_wave_state_changed(self, data: dict) -> None:
        plan = self._plans[data["plan_id"]]
        wave = self._find_wave(plan, data["wave_id"])
        assert wave is not None
        to_state = RolloutState(data["to_state"])
        wave.transitions.append(Transition(
            data["from_state"], data["to_state"], data["ts"],
            data.get("reason", ""), data.get("decision_id", "")))
        wave.state = to_state
        if to_state in TERMINAL_WAVE_STATES:
            wave.closed_at = data["ts"]
            if data.get("decision_id"):
                wave.final_decision_id = data["decision_id"]

    def _on_plan_state_changed(self, data: dict) -> None:
        plan = self._plans[data["plan_id"]]
        plan.transitions.append(Transition(
            data["from_state"], data["to_state"], data["ts"],
            data.get("reason", ""), data.get("decision_id", "")))
        plan.state = PlanState(data["to_state"])

    def _on_batch_quarantined(self, data: dict) -> None:
        batch = data["hardware_batch"]
        existing = self._quarantines.get(batch)
        if existing is None or existing.cleared:
            self._quarantines[batch] = QuarantineRecord(
                hardware_batch=batch,
                plan_id=data["plan_id"],
                reason=data.get("reason", ""),
                decision_id=data.get("decision_id", ""),
                quarantined_at=data["ts"],
            )
        plan = self._plans[data["plan_id"]]
        superseded = set(data.get("superseded_commands", []))
        for command_id in superseded:
            self._commands[command_id].status = CommandStatus.SUPERSEDED
        for bump in data.get("epoch_bumps", []):
            self._vehicles[bump["vehicle_id"]].epoch = bump["new_epoch"]
        quarantined = set(data.get("quarantined_vehicles", []))
        for wave in plan.waves:
            for vid in quarantined:
                assignment = wave.cohort.get(vid)
                if assignment is not None \
                        and assignment.status in ACTIVE_ASSIGNMENT_STATUSES:
                    assignment.status = VehicleStatus.QUARANTINED

    def _on_rollback_started(self, data: dict) -> None:
        plan = self._plans[data["plan_id"]]
        rollback_wave_of = {}
        for command_data in data.get("rollback_commands", []):
            command = CommandRecord.from_dict(command_data)
            self._commands[command.command_id] = command
            rollback_wave_of[command.vehicle_id] = command.wave_id
        for command_id in data.get("superseded_commands", []):
            self._commands[command_id].status = CommandStatus.SUPERSEDED
        for bump in data.get("epoch_bumps", []):
            self._vehicles[bump["vehicle_id"]].epoch = bump["new_epoch"]
        excluded = {e["vehicle_id"]: e["reason"] for e in data.get("excluded", [])}
        for wave in plan.waves:
            for vid, assignment in wave.cohort.items():
                if rollback_wave_of.get(vid) == wave.wave_id:
                    assignment.status = VehicleStatus.ROLLING_BACK
                    assignment.attempts = 1
                elif vid in excluded \
                        and assignment.status in ACTIVE_ASSIGNMENT_STATUSES:
                    assignment.status = VehicleStatus.EXCLUDED
                    assignment.reasons.append(excluded[vid])

    def _on_plan_policy_migrated(self, data: dict) -> None:
        plan = self._plans[data["plan_id"]]
        plan.transitions.append(Transition(
            plan.state.value, plan.state.value, data["ts"],
            f"规则版本 {data['from_version']} → {data['to_version']}: "
            f"{data.get('reason', '')}"))
        plan.policy_version = data["to_version"]

    def _on_quarantine_cleared(self, data: dict) -> None:
        record = self._quarantines[data["hardware_batch"]]
        record.cleared = True
        record.cleared_at = data["ts"]
        record.cleared_by = data.get("operator", "")

    # ------------------------------------------------------------------
    # 序列化辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _package_dict(package: SoftwarePackage) -> dict:
        return {
            "package_id": package.package_id,
            "version": package.version,
            "description": package.description,
            "compatibility": {
                "hardware_batches": sorted(package.compatibility.hardware_batches),
                "source_versions": sorted(package.compatibility.source_versions),
                "min_battery_percent": package.compatibility.min_battery_percent,
                "require_online": package.compatibility.require_online,
                "models": sorted(package.compatibility.models),
            },
        }

    @staticmethod
    def _policy_dict(policy: RiskPolicy) -> dict:
        return {
            "version": policy.version,
            "pause_failure_rate": policy.pause_failure_rate,
            "rollback_failure_rate": policy.rollback_failure_rate,
            "batch_quarantine_rate": policy.batch_quarantine_rate,
            "min_batch_sample": policy.min_batch_sample,
            "min_wave_sample": policy.min_wave_sample,
            "unhealthy_score_below": policy.unhealthy_score_below,
            "pause_incident_count": policy.pause_incident_count,
            "rollback_incident_count": policy.rollback_incident_count,
        }

    @staticmethod
    def _vehicle_dict(snapshot: VehicleSnapshot) -> dict:
        return {
            "vehicle_id": snapshot.vehicle_id,
            "hardware_batch": snapshot.hardware_batch,
            "software_version": snapshot.software_version,
            "battery_percent": snapshot.battery_percent,
            "online": snapshot.online,
            "model": snapshot.model,
        }

"""灰度发布控制服务的核心应用逻辑。

职责：
- 登记软件包 / 兼容条件 / 车辆快照 / 风险策略（版本化）/ 发布波次；
- 审批后按小流量逐级放量，为每辆车下发幂等安装命令；
- 根据迟到 / 重复回执、健康指标与人工事件自动决策继续、暂停或完结；
- 支持人工暂停、恢复（含审计的强制恢复）、回滚与隔离；
- 回滚车辆被封锁，不得被旧波次重新推进；
- 所有决策追加式记录并固化策略版本，规则换版不会改写已完成的决策。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, Optional

from .contracts import (
    FINAL_COMMAND_STATUSES,
    TERMINAL_WAVE_STATES,
    CommandKind,
    CommandStatus,
    Compatibility,
    DecisionAction,
    DecisionRecord,
    IncidentSeverity,
    MemberRationale,
    ReceiptStatus,
    RecoveryCondition,
    RiskBudget,
    RiskPolicy,
    RolloutState,
    SoftwarePackage,
    TransitionRecord,
    VehicleSnapshot,
    WaveDetail,
)
from .storage import Store

DEFAULT_POLICY_ID = "default"

# 允许的状态迁移表
ALLOWED_TRANSITIONS: dict[RolloutState, frozenset[RolloutState]] = {
    RolloutState.AWAITING_APPROVAL: frozenset({RolloutState.APPROVED}),
    RolloutState.APPROVED: frozenset({RolloutState.RUNNING}),
    RolloutState.RUNNING: frozenset(
        {
            RolloutState.PAUSED,
            RolloutState.COMPLETED,
            RolloutState.ROLLED_BACK,
            RolloutState.QUARANTINED,
        }
    ),
    RolloutState.PAUSED: frozenset(
        {RolloutState.RUNNING, RolloutState.ROLLED_BACK, RolloutState.QUARANTINED}
    ),
    RolloutState.COMPLETED: frozenset(
        {RolloutState.ROLLED_BACK, RolloutState.QUARANTINED}
    ),
    RolloutState.ROLLED_BACK: frozenset(),
    RolloutState.QUARANTINED: frozenset(),
}


class ServiceError(Exception):
    """服务层错误，status_code 供 HTTP 层映射。"""

    status_code = 400


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class RolloutControlService:
    """车端软件灰度发布控制服务。实例无状态，可随时重建（服务重启）。"""

    def __init__(
        self,
        db_path: str,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.store = Store(db_path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        # 保证内置默认策略存在（幂等）
        self.register_policy(RiskPolicy(policy_id=DEFAULT_POLICY_ID, version=1))

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now_iso(self) -> str:
        return self._now().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _require_state(wave: sqlite3.Row, expected: RolloutState) -> None:
        if wave["state"] != expected.value:
            raise ConflictError(
                f"波次 {wave['wave_id']} 当前状态为 {wave['state']}，"
                f"无法执行需要 {expected.value} 状态的操作"
            )

    def _wave_row(self, conn: sqlite3.Connection, wave_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM waves WHERE wave_id=?", (wave_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"波次 {wave_id} 不存在")
        return row

    def _package_row(self, conn: sqlite3.Connection, package_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"软件包 {package_id} 不存在")
        return row

    def _policy(self, conn: sqlite3.Connection, policy_id: str, version: int) -> RiskPolicy:
        row = conn.execute(
            "SELECT rules_json FROM policies WHERE policy_id=? AND version=?",
            (policy_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"风险策略 {policy_id}@{version} 不存在")
        return RiskPolicy(**json.loads(row["rules_json"]))

    def _anomaly(self, conn: sqlite3.Connection, kind: str, ref_id: str, detail: str) -> None:
        conn.execute(
            "INSERT INTO anomalies (kind, ref_id, detail, at) VALUES (?,?,?,?)",
            (kind, ref_id, detail, self._now_iso()),
        )

    def _transition(
        self,
        conn: sqlite3.Connection,
        wave: sqlite3.Row,
        to_state: RolloutState,
        reason: str,
        actor: str,
    ) -> None:
        from_state = RolloutState(wave["state"])
        if to_state not in ALLOWED_TRANSITIONS[from_state]:
            raise ConflictError(
                f"波次 {wave['wave_id']} 不允许从 {from_state.value} 迁移到 {to_state.value}"
            )
        now_iso = self._now_iso()
        started = wave["started_at"]
        if to_state == RolloutState.RUNNING and not started:
            started = now_iso
        conn.execute(
            "UPDATE waves SET state=?, started_at=?, updated_at=? WHERE wave_id=?",
            (to_state.value, started, now_iso, wave["wave_id"]),
        )
        conn.execute(
            "INSERT INTO transitions (wave_id, from_state, to_state, reason, actor, at)"
            " VALUES (?,?,?,?,?,?)",
            (wave["wave_id"], from_state.value, to_state.value, reason, actor, now_iso),
        )

    # ------------------------------------------------------------------
    # 登记：软件包 / 风险策略 / 车辆快照
    # ------------------------------------------------------------------
    def register_package(self, package: SoftwarePackage) -> dict:
        compat_json = json.dumps(asdict(package.compatibility), ensure_ascii=False, sort_keys=True)
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT model, version, compatibility_json FROM packages WHERE package_id=?",
                (package.package_id,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["model"] == package.model
                    and existing["version"] == package.version
                    and existing["compatibility_json"] == compat_json
                )
                if not same:
                    raise ConflictError(
                        f"软件包 {package.package_id} 已登记且内容不一致，禁止覆盖登记"
                    )
                return {"package_id": package.package_id, "registered": False}
            conn.execute(
                "INSERT INTO packages (package_id, model, version, compatibility_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (package.package_id, package.model, package.version, compat_json, self._now_iso()),
            )
        return {"package_id": package.package_id, "registered": True}

    def register_policy(self, policy: RiskPolicy) -> dict:
        rules_json = json.dumps(asdict(policy), ensure_ascii=False, sort_keys=True)
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT rules_json FROM policies WHERE policy_id=? AND version=?",
                (policy.policy_id, policy.version),
            ).fetchone()
            if existing is not None:
                if existing["rules_json"] != rules_json:
                    raise ConflictError(
                        f"风险策略 {policy.policy_id}@{policy.version} 已存在且内容不一致："
                        "规则换版必须递增版本号，禁止静默修改"
                    )
                return {"policy_id": policy.policy_id, "version": policy.version, "registered": False}
            conn.execute(
                "INSERT INTO policies (policy_id, version, rules_json, created_at) VALUES (?,?,?,?)",
                (policy.policy_id, policy.version, rules_json, self._now_iso()),
            )
        return {"policy_id": policy.policy_id, "version": policy.version, "registered": True}

    def register_snapshot(self, snapshot: VehicleSnapshot) -> dict:
        reported_at = snapshot.reported_at or self._now_iso()
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT reported_at FROM snapshots WHERE vehicle_id=?", (snapshot.vehicle_id,)
            ).fetchone()
            if existing is not None and existing["reported_at"] > reported_at:
                # 迟到的旧快照不覆盖新数据（车辆状态回报可能不同步）
                return {"vehicle_id": snapshot.vehicle_id, "updated": False, "reason": "过期的快照上报被忽略"}
            conn.execute(
                "INSERT OR REPLACE INTO snapshots"
                " (vehicle_id, model, hardware_batch, software_version, battery_percent,"
                "  online, health_score, reported_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    snapshot.vehicle_id,
                    snapshot.model,
                    snapshot.hardware_batch,
                    snapshot.software_version,
                    snapshot.battery_percent,
                    int(snapshot.online),
                    snapshot.health_score,
                    reported_at,
                    self._now_iso(),
                ),
            )
        return {"vehicle_id": snapshot.vehicle_id, "updated": True}

    # ------------------------------------------------------------------
    # 波次生命周期
    # ------------------------------------------------------------------
    def create_wave(
        self,
        wave_id: str,
        package_id: str,
        seq: int,
        target_percent: float,
        policy_id: str = DEFAULT_POLICY_ID,
        actor: str = "system",
    ) -> WaveDetail:
        if not 0 < target_percent <= 1:
            raise ServiceError("目标放量比例必须位于 (0, 1] 区间")
        if seq < 1:
            raise ServiceError("波次序号必须为正整数")
        with self.store.write() as conn:
            self._package_row(conn, package_id)
            policy_row = conn.execute(
                "SELECT MAX(version) AS v FROM policies WHERE policy_id=?", (policy_id,)
            ).fetchone()
            if policy_row is None or policy_row["v"] is None:
                raise NotFoundError(f"风险策略 {policy_id} 不存在")
            now_iso = self._now_iso()
            try:
                conn.execute(
                    "INSERT INTO waves"
                    " (wave_id, package_id, seq, target_percent, state, policy_id,"
                    "  policy_version, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        wave_id,
                        package_id,
                        seq,
                        target_percent,
                        RolloutState.AWAITING_APPROVAL.value,
                        policy_id,
                        policy_row["v"],
                        now_iso,
                        now_iso,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"波次标识或序号冲突：{exc}") from exc
            conn.execute(
                "INSERT INTO transitions (wave_id, from_state, to_state, reason, actor, at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    wave_id,
                    "",
                    RolloutState.AWAITING_APPROVAL.value,
                    f"波次创建，固化策略 {policy_id}@{policy_row['v']}",
                    actor,
                    now_iso,
                ),
            )
        return self.get_wave(wave_id)

    def approve_wave(self, wave_id: str, actor: str = "ops") -> WaveDetail:
        with self.store.write() as conn:
            wave = self._wave_row(conn, wave_id)
            self._require_state(wave, RolloutState.AWAITING_APPROVAL)
            self._transition(conn, wave, RolloutState.APPROVED, "审批通过", actor)
        return self.get_wave(wave_id)

    def start_wave(self, wave_id: str, actor: str = "ops") -> WaveDetail:
        """审批后启动波次：按兼容条件与车辆标记选车，下发幂等安装命令。"""
        with self.store.write() as conn:
            wave = self._wave_row(conn, wave_id)
            self._require_state(wave, RolloutState.APPROVED)
            package = self._package_row(conn, wave["package_id"])
            compat = Compatibility(**json.loads(package["compatibility_json"]))
            package_id = wave["package_id"]

            # 逐级放量闸门：前序波次必须完结（完成或已回滚）
            blockers = conn.execute(
                "SELECT wave_id, state FROM waves WHERE package_id=? AND seq<?"
                " AND state NOT IN (?, ?)",
                (
                    package_id,
                    wave["seq"],
                    RolloutState.COMPLETED.value,
                    RolloutState.ROLLED_BACK.value,
                ),
            ).fetchall()
            if blockers:
                desc = "、".join(f"{b['wave_id']}({b['state']})" for b in blockers)
                raise ConflictError(f"前序波次未完结，禁止放量：{desc}")

            now_dt = self._now()
            fleet = conn.execute(
                "SELECT * FROM snapshots WHERE model=? ORDER BY vehicle_id",
                (package["model"],),
            ).fetchall()
            if not fleet:
                raise ConflictError(f"车型 {package['model']} 暂无车辆快照，无法放量")

            prior_members = {
                r["vehicle_id"]: r["wave_id"]
                for r in conn.execute(
                    "SELECT m.vehicle_id, m.wave_id FROM wave_members m"
                    " JOIN waves w ON w.wave_id=m.wave_id"
                    " WHERE w.package_id=? AND w.seq<? AND m.included=1",
                    (package_id, wave["seq"]),
                ).fetchall()
            }
            flags = {
                r["vehicle_id"]: r
                for r in conn.execute("SELECT * FROM vehicle_flags").fetchall()
            }

            target_cum = math.ceil(len(fleet) * wave["target_percent"])
            needed = max(0, target_cum - len(prior_members))

            selected: list[str] = []
            evaluations: list[tuple[str, int, list[str]]] = []
            for snap in fleet:
                vid = snap["vehicle_id"]
                # 先查隔离 / 回滚封锁等硬性标记，理由更贴近根因
                ok, reasons = self._check_eligibility(
                    snap, compat, flags.get(vid), package_id, now_dt
                )
                if not ok:
                    evaluations.append((vid, 0, reasons))
                    continue
                if vid in prior_members:
                    evaluations.append(
                        (vid, 0, reasons + [f"已在波次 {prior_members[vid]} 中推送过，本次跳过"])
                    )
                    continue
                if len(selected) < needed:
                    selected.append(vid)
                    evaluations.append((vid, 1, reasons + ["通过全部兼容检查，入选本波次"]))
                else:
                    evaluations.append((vid, 0, reasons + ["本波次名额已满，留待后续波次"]))

            conn.executemany(
                "INSERT INTO wave_members (wave_id, vehicle_id, included, reasons_json)"
                " VALUES (?,?,?,?)",
                [
                    (wave_id, vid, inc, json.dumps(reasons, ensure_ascii=False))
                    for vid, inc, reasons in evaluations
                ],
            )
            now_iso = self._now_iso()
            for vid in selected:
                # 命令标识确定性生成 + 唯一约束，重复下发保持幂等
                conn.execute(
                    "INSERT OR IGNORE INTO commands"
                    " (command_id, wave_id, vehicle_id, package_id, kind, status, issued_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"cmd-{wave_id}-{vid}-install",
                        wave_id,
                        vid,
                        package_id,
                        CommandKind.INSTALL.value,
                        CommandStatus.ISSUED.value,
                        now_iso,
                        now_iso,
                    ),
                )
            self._transition(
                conn, wave, RolloutState.RUNNING, f"波次启动，入选 {len(selected)} 辆", actor
            )
            # 入选为零时立即评估，波次直接完结
            self._evaluate(conn, self._wave_row(conn, wave_id))
        return self.get_wave(wave_id)

    def pause_wave(self, wave_id: str, actor: str = "ops", reason: str = "人工暂停") -> WaveDetail:
        with self.store.write() as conn:
            wave = self._wave_row(conn, wave_id)
            self._require_state(wave, RolloutState.RUNNING)
            self._transition(conn, wave, RolloutState.PAUSED, reason, actor)
        return self.get_wave(wave_id)

    def resume_wave(
        self, wave_id: str, actor: str = "ops", override_reason: str = ""
    ) -> WaveDetail:
        """恢复暂停的波次；恢复条件未满足时必须给出强制理由（记录审计）。"""
        with self.store.write() as conn:
            wave = self._wave_row(conn, wave_id)
            self._require_state(wave, RolloutState.PAUSED)
            conditions = self._recovery_conditions(conn, wave)
            unmet = [c for c in conditions if not c.met]
            if unmet and not override_reason:
                names = "、".join(c.name for c in unmet)
                raise ConflictError(f"恢复条件未满足：{names}；如需强制恢复请提供理由")
            if unmet:
                names = "、".join(c.name for c in unmet)
                reason = f"人工强制恢复（未满足：{names}）：{override_reason}"
                conn.execute(
                    "UPDATE waves SET manual_override=1 WHERE wave_id=?", (wave_id,)
                )
            else:
                reason = "恢复条件已满足，人工恢复"
            self._transition(conn, wave, RolloutState.RUNNING, reason, actor)
        return self.get_wave(wave_id)

    def rollback_wave(self, wave_id: str, actor: str = "ops", reason: str = "人工回滚") -> WaveDetail:
        """回滚波次：已装车辆下发幂等回滚命令，所有成员封锁该软件包。"""
        with self.store.write() as conn:
            wave = self._wave_row(conn, wave_id)
            if wave["state"] not in (
                RolloutState.RUNNING.value,
                RolloutState.PAUSED.value,
                RolloutState.COMPLETED.value,
            ):
                raise ConflictError(f"波次 {wave_id} 状态为 {wave['state']}，无法回滚")
            package_id = wave["package_id"]
            members = [
                r["vehicle_id"]
                for r in conn.execute(
                    "SELECT vehicle_id FROM wave_members WHERE wave_id=? AND included=1",
                    (wave_id,),
                ).fetchall()
            ]
            now_iso = self._now_iso()
            for vid in members:
                self._block_package(conn, vid, package_id, now_iso)
            # 未完结的安装命令取消，回执到达时按迟到/无效处理
            conn.execute(
                "UPDATE commands SET status=?, updated_at=?"
                " WHERE wave_id=? AND kind=? AND status=?",
                (
                    CommandStatus.CANCELLED.value,
                    now_iso,
                    wave_id,
                    CommandKind.INSTALL.value,
                    CommandStatus.ISSUED.value,
                ),
            )
            # 对已安装车辆下发幂等回滚命令
            for row in conn.execute(
                "SELECT vehicle_id FROM commands WHERE wave_id=? AND kind=? AND status=?",
                (wave_id, CommandKind.INSTALL.value, CommandStatus.SUCCEEDED.value),
            ).fetchall():
                vid = row["vehicle_id"]
                conn.execute(
                    "INSERT OR IGNORE INTO commands"
                    " (command_id, wave_id, vehicle_id, package_id, kind, status, issued_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"cmd-{wave_id}-{vid}-rollback",
                        wave_id,
                        vid,
                        package_id,
                        CommandKind.ROLLBACK.value,
                        CommandStatus.ISSUED.value,
                        now_iso,
                        now_iso,
                    ),
                )
            self._transition(conn, wave, RolloutState.ROLLED_BACK, reason, actor)
        return self.get_wave(wave_id)

    def quarantine_wave(self, wave_id: str, actor: str = "ops", reason: str = "人工隔离") -> WaveDetail:
        """隔离波次：中止推送并将全部成员车辆隔离，排除出后续所有波次。"""
        with self.store.write() as conn:
            wave = self._wave_row(conn, wave_id)
            if wave["state"] not in (
                RolloutState.RUNNING.value,
                RolloutState.PAUSED.value,
                RolloutState.COMPLETED.value,
            ):
                raise ConflictError(f"波次 {wave_id} 状态为 {wave['state']}，无法隔离")
            now_iso = self._now_iso()
            members = conn.execute(
                "SELECT vehicle_id FROM wave_members WHERE wave_id=? AND included=1",
                (wave_id,),
            ).fetchall()
            for row in members:
                self._quarantine_vehicle(
                    conn, row["vehicle_id"], f"波次 {wave_id} 被隔离：{reason}", now_iso
                )
            conn.execute(
                "UPDATE commands SET status=?, updated_at=?"
                " WHERE wave_id=? AND status=?",
                (CommandStatus.CANCELLED.value, now_iso, wave_id, CommandStatus.ISSUED.value),
            )
            self._transition(conn, wave, RolloutState.QUARANTINED, reason, actor)
        return self.get_wave(wave_id)

    def quarantine_vehicle(self, vehicle_id: str, reason: str, actor: str = "ops") -> dict:
        with self.store.write() as conn:
            self._snapshot_row(conn, vehicle_id)
            self._quarantine_vehicle(conn, vehicle_id, reason, self._now_iso())
            # 取消该车未完结命令并重新评估相关波次
            conn.execute(
                "UPDATE commands SET status=?, updated_at=? WHERE vehicle_id=? AND status=?",
                (CommandStatus.CANCELLED.value, self._now_iso(), vehicle_id, CommandStatus.ISSUED.value),
            )
            for wave in self._running_waves_of(conn, vehicle_id):
                self._evaluate(conn, wave)
        return {"vehicle_id": vehicle_id, "quarantined": True, "reason": reason}

    # ------------------------------------------------------------------
    # 信号接入：回执 / 人工事件 / 健康回报
    # ------------------------------------------------------------------
    def submit_receipt(
        self,
        receipt_id: str,
        command_id: str,
        status: str,
        detail: str = "",
        reported_at: str = "",
    ) -> dict:
        """接入安装回执。重复回执幂等去重，迟到回执标记但不改写已完成决策。"""
        try:
            status_enum = ReceiptStatus(status)
        except ValueError as exc:
            raise ServiceError(f"非法回执状态：{status}") from exc
        with self.store.write() as conn:
            cmd = conn.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if cmd is None:
                raise NotFoundError(f"安装命令 {command_id} 不存在")
            existing = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if existing is not None:
                conflict = (
                    existing["command_id"] != command_id
                    or existing["status"] != status_enum.value
                )
                if conflict:
                    self._anomaly(
                        conn,
                        "receipt_conflict",
                        receipt_id,
                        f"回执冲突：已存 {existing['command_id']}/{existing['status']}，"
                        f"新报 {command_id}/{status_enum.value}，保留先到的回执",
                    )
                return {
                    "receipt_id": receipt_id,
                    "duplicate": True,
                    "conflict": conflict,
                    "late": bool(existing["late"]),
                }
            wave = self._wave_row(conn, cmd["wave_id"])
            policy = self._policy(conn, wave["policy_id"], wave["policy_version"])
            now_dt = self._now()
            now_iso = now_dt.astimezone(timezone.utc).isoformat()
            age = (now_dt - _parse_dt(cmd["issued_at"])).total_seconds()
            terminal = wave["state"] in {s.value for s in TERMINAL_WAVE_STATES}
            late = age > policy.receipt_timeout_seconds or terminal
            cmd_final = cmd["status"] in {s.value for s in FINAL_COMMAND_STATUSES}
            conn.execute(
                "INSERT INTO receipts"
                " (receipt_id, command_id, wave_id, vehicle_id, status, detail,"
                "  late, duplicate, reported_at, received_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    command_id,
                    cmd["wave_id"],
                    cmd["vehicle_id"],
                    status_enum.value,
                    detail,
                    int(late),
                    int(cmd_final),
                    reported_at or now_iso,
                    now_iso,
                ),
            )
            if not cmd_final:
                new_status = (
                    CommandStatus.SUCCEEDED
                    if status_enum == ReceiptStatus.SUCCESS
                    else CommandStatus.FAILED
                )
                conn.execute(
                    "UPDATE commands SET status=?, updated_at=? WHERE command_id=?",
                    (new_status.value, now_iso, command_id),
                )
            if wave["state"] == RolloutState.RUNNING.value:
                self._evaluate(conn, wave)
            return {
                "receipt_id": receipt_id,
                "duplicate": bool(cmd_final),
                "conflict": False,
                "late": late,
            }

    def report_incident(
        self,
        incident_id: str,
        vehicle_id: str,
        severity: str,
        summary: str = "",
        reported_at: str = "",
    ) -> dict:
        """人工事件报告，按 vehicle_id 幂等去重并触发相关波次评估。"""
        try:
            severity_enum = IncidentSeverity(severity)
        except ValueError as exc:
            raise ServiceError(f"非法事件级别：{severity}") from exc
        with self.store.write() as conn:
            self._snapshot_row(conn, vehicle_id)
            existing = conn.execute(
                "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
            if existing is not None:
                conflict = (
                    existing["vehicle_id"] != vehicle_id
                    or existing["severity"] != severity_enum.value
                )
                if conflict:
                    self._anomaly(
                        conn,
                        "incident_conflict",
                        incident_id,
                        f"事件冲突：已存 {existing['vehicle_id']}/{existing['severity']}，"
                        f"新报 {vehicle_id}/{severity_enum.value}",
                    )
                return {"incident_id": incident_id, "duplicate": True, "conflict": conflict}
            now_iso = self._now_iso()
            conn.execute(
                "INSERT INTO incidents (incident_id, vehicle_id, severity, summary, reported_at, received_at)"
                " VALUES (?,?,?,?,?,?)",
                (incident_id, vehicle_id, severity_enum.value, summary, reported_at or now_iso, now_iso),
            )
            for wave in self._running_waves_of(conn, vehicle_id):
                self._evaluate(conn, wave)
        return {"incident_id": incident_id, "duplicate": False, "conflict": False}

    def report_health(self, vehicle_id: str, score: float, reported_at: str = "") -> dict:
        if not 0.0 <= score <= 1.0:
            raise ServiceError("健康分必须位于零到一之间")
        with self.store.write() as conn:
            self._snapshot_row(conn, vehicle_id)
            now_iso = self._now_iso()
            reported = reported_at or now_iso
            cursor = conn.execute(
                "INSERT OR IGNORE INTO health_reports (vehicle_id, reported_at, score, received_at)"
                " VALUES (?,?,?,?)",
                (vehicle_id, reported, score, now_iso),
            )
            if cursor.rowcount == 0:
                existing = conn.execute(
                    "SELECT score FROM health_reports WHERE vehicle_id=? AND reported_at=?",
                    (vehicle_id, reported),
                ).fetchone()
                if existing is not None and existing["score"] != score:
                    self._anomaly(
                        conn,
                        "health_conflict",
                        f"{vehicle_id}@{reported}",
                        f"健康回报冲突：已存 {existing['score']}，新报 {score}",
                    )
                return {"vehicle_id": vehicle_id, "updated": False}
            # 同步最新健康分到快照，供入组校验使用
            conn.execute(
                "UPDATE snapshots SET health_score=?, updated_at=? WHERE vehicle_id=?",
                (score, now_iso, vehicle_id),
            )
            for wave in self._running_waves_of(conn, vehicle_id):
                self._evaluate(conn, wave)
        return {"vehicle_id": vehicle_id, "updated": True}

    # ------------------------------------------------------------------
    # 运维查询
    # ------------------------------------------------------------------
    def get_wave(self, wave_id: str) -> WaveDetail:
        with self.store.read() as conn:
            wave = self._wave_row(conn, wave_id)
            return self._wave_detail(conn, wave)

    def list_waves(self, package_id: str = "") -> list[WaveDetail]:
        with self.store.read() as conn:
            if package_id:
                rows = conn.execute(
                    "SELECT * FROM waves WHERE package_id=? ORDER BY package_id, seq",
                    (package_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM waves ORDER BY package_id, seq"
                ).fetchall()
            return [self._wave_detail(conn, r) for r in rows]

    def get_members(self, wave_id: str) -> list[MemberRationale]:
        with self.store.read() as conn:
            self._wave_row(conn, wave_id)
            rows = conn.execute(
                "SELECT * FROM wave_members WHERE wave_id=? ORDER BY vehicle_id",
                (wave_id,),
            ).fetchall()
            return [
                MemberRationale(
                    wave_id=wave_id,
                    vehicle_id=r["vehicle_id"],
                    included=bool(r["included"]),
                    reasons=tuple(json.loads(r["reasons_json"])),
                )
                for r in rows
            ]

    def get_risk_budget(self, wave_id: str) -> RiskBudget:
        with self.store.read() as conn:
            wave = self._wave_row(conn, wave_id)
            policy = self._policy(conn, wave["policy_id"], wave["policy_version"])
            metrics = self._compute_metrics(conn, wave, policy)
            breaches = self._breaches(metrics, policy)
            return RiskBudget(
                wave_id=wave_id,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                members=metrics["members"],
                commands_issued=metrics["issued"],
                receipts_received=metrics["received"],
                succeeded=metrics["succeeded"],
                failed=metrics["failed"],
                cancelled=metrics["cancelled"],
                pending=metrics["pending"],
                late_receipts=metrics["late_receipts"],
                failure_rate=round(metrics["failure_rate"], 6),
                allowed_failures=metrics["allowed_failures"],
                failures_remaining=metrics["allowed_failures"] - metrics["failed"],
                critical_incidents=metrics["critical_incidents"],
                max_critical_incidents=policy.max_critical_incidents,
                total_incidents=metrics["total_incidents"],
                max_incidents=policy.max_incidents,
                avg_health_score=round(metrics["avg_health_score"], 6),
                min_avg_health_score=policy.min_avg_health_score,
                late_ratio=round(metrics["late_ratio"], 6),
                max_late_ratio=policy.max_late_ratio,
                within_budget=not breaches,
                breaches=tuple(breaches),
            )

    def get_transitions(self, wave_id: str) -> list[TransitionRecord]:
        with self.store.read() as conn:
            self._wave_row(conn, wave_id)
            rows = conn.execute(
                "SELECT * FROM transitions WHERE wave_id=? ORDER BY id", (wave_id,)
            ).fetchall()
            return [
                TransitionRecord(
                    transition_id=r["id"],
                    wave_id=wave_id,
                    from_state=r["from_state"],
                    to_state=r["to_state"],
                    reason=r["reason"],
                    actor=r["actor"],
                    at=r["at"],
                )
                for r in rows
            ]

    def get_decisions(self, wave_id: str) -> list[DecisionRecord]:
        with self.store.read() as conn:
            self._wave_row(conn, wave_id)
            rows = conn.execute(
                "SELECT * FROM decisions WHERE wave_id=? ORDER BY id", (wave_id,)
            ).fetchall()
            return [
                DecisionRecord(
                    decision_id=r["id"],
                    wave_id=wave_id,
                    action=r["action"],
                    policy_id=r["policy_id"],
                    policy_version=r["policy_version"],
                    metrics=json.loads(r["metrics_json"]),
                    reason=r["reason"],
                    at=r["at"],
                )
                for r in rows
            ]

    def get_recovery_conditions(self, wave_id: str) -> list[RecoveryCondition]:
        with self.store.read() as conn:
            wave = self._wave_row(conn, wave_id)
            return self._recovery_conditions(conn, wave)

    def get_vehicle(self, vehicle_id: str) -> dict:
        with self.store.read() as conn:
            snap = self._snapshot_row(conn, vehicle_id)
            flag = conn.execute(
                "SELECT * FROM vehicle_flags WHERE vehicle_id=?", (vehicle_id,)
            ).fetchone()
            commands = conn.execute(
                "SELECT command_id, wave_id, kind, status, issued_at, updated_at"
                " FROM commands WHERE vehicle_id=? ORDER BY issued_at",
                (vehicle_id,),
            ).fetchall()
            return {
                "vehicle_id": vehicle_id,
                "snapshot": {
                    "model": snap["model"],
                    "hardware_batch": snap["hardware_batch"],
                    "software_version": snap["software_version"],
                    "battery_percent": snap["battery_percent"],
                    "online": bool(snap["online"]),
                    "health_score": snap["health_score"],
                    "reported_at": snap["reported_at"],
                },
                "flags": {
                    "quarantined": bool(flag["quarantined"]) if flag else False,
                    "quarantine_reason": flag["quarantine_reason"] if flag else "",
                    "blocked_packages": json.loads(flag["blocked_packages_json"]) if flag else [],
                },
                "commands": [dict(c) for c in commands],
            }

    def list_commands(self, wave_id: str) -> list[dict]:
        with self.store.read() as conn:
            self._wave_row(conn, wave_id)
            rows = conn.execute(
                "SELECT * FROM commands WHERE wave_id=? ORDER BY command_id", (wave_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_receipts(self, wave_id: str) -> list[dict]:
        with self.store.read() as conn:
            self._wave_row(conn, wave_id)
            rows = conn.execute(
                "SELECT * FROM receipts WHERE wave_id=? ORDER BY received_at, receipt_id",
                (wave_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_package(self, package_id: str) -> dict:
        with self.store.read() as conn:
            row = self._package_row(conn, package_id)
            return {
                "package_id": row["package_id"],
                "model": row["model"],
                "version": row["version"],
                "compatibility": json.loads(row["compatibility_json"]),
                "created_at": row["created_at"],
            }

    def list_packages(self) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT package_id FROM packages ORDER BY package_id").fetchall()
            return [self.get_package(r["package_id"]) for r in rows]

    def list_policies(self) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT rules_json, created_at FROM policies ORDER BY policy_id, version"
            ).fetchall()
            return [
                {**json.loads(r["rules_json"]), "created_at": r["created_at"]} for r in rows
            ]

    def list_anomalies(self) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT * FROM anomalies ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 内部：资格校验 / 风险评估 / 恢复条件
    # ------------------------------------------------------------------
    def _snapshot_row(self, conn: sqlite3.Connection, vehicle_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE vehicle_id=?", (vehicle_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"车辆 {vehicle_id} 未登记快照")
        return row

    def _running_waves_of(self, conn: sqlite3.Connection, vehicle_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT w.* FROM waves w JOIN wave_members m ON m.wave_id=w.wave_id"
            " WHERE m.vehicle_id=? AND m.included=1 AND w.state=?",
            (vehicle_id, RolloutState.RUNNING.value),
        ).fetchall()

    def _block_package(
        self, conn: sqlite3.Connection, vehicle_id: str, package_id: str, now_iso: str
    ) -> None:
        row = conn.execute(
            "SELECT blocked_packages_json FROM vehicle_flags WHERE vehicle_id=?", (vehicle_id,)
        ).fetchone()
        blocked = set(json.loads(row["blocked_packages_json"])) if row else set()
        blocked.add(package_id)
        conn.execute(
            "INSERT INTO vehicle_flags (vehicle_id, quarantined, quarantine_reason,"
            " blocked_packages_json, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(vehicle_id) DO UPDATE SET"
            " blocked_packages_json=excluded.blocked_packages_json, updated_at=excluded.updated_at",
            (vehicle_id, 0, "", json.dumps(sorted(blocked)), now_iso),
        )

    def _quarantine_vehicle(
        self, conn: sqlite3.Connection, vehicle_id: str, reason: str, now_iso: str
    ) -> None:
        conn.execute(
            "INSERT INTO vehicle_flags (vehicle_id, quarantined, quarantine_reason,"
            " blocked_packages_json, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(vehicle_id) DO UPDATE SET"
            " quarantined=1, quarantine_reason=excluded.quarantine_reason,"
            " updated_at=excluded.updated_at",
            (vehicle_id, 1, reason, "[]", now_iso),
        )

    def _check_eligibility(
        self,
        snap: sqlite3.Row,
        compat: Compatibility,
        flag_row: Optional[sqlite3.Row],
        package_id: str,
        now_dt: datetime,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        ok = True
        if compat.allowed_hardware_batches:
            if snap["hardware_batch"] in compat.allowed_hardware_batches:
                reasons.append(f"硬件批次 {snap['hardware_batch']} 在兼容列表内")
            else:
                ok = False
                reasons.append(
                    f"硬件批次 {snap['hardware_batch']} 不在兼容列表"
                    f" {list(compat.allowed_hardware_batches)} 内"
                )
        if compat.allowed_from_versions:
            if snap["software_version"] in compat.allowed_from_versions:
                reasons.append(f"当前版本 {snap['software_version']} 可升级")
            else:
                ok = False
                reasons.append(
                    f"当前版本 {snap['software_version']} 不在可升级版本"
                    f" {list(compat.allowed_from_versions)} 内"
                )
        if snap["battery_percent"] >= compat.min_battery_percent:
            reasons.append(
                f"电量 {snap['battery_percent']}% 满足最低要求 {compat.min_battery_percent}%"
            )
        else:
            ok = False
            reasons.append(
                f"电量 {snap['battery_percent']}% 低于最低要求 {compat.min_battery_percent}%"
            )
        if compat.require_online:
            if snap["online"]:
                reasons.append("车辆在线")
            else:
                ok = False
                reasons.append("车辆离线，快照显示不在线")
        if snap["health_score"] >= compat.min_health_score:
            reasons.append(f"健康分 {snap['health_score']} 满足最低要求 {compat.min_health_score}")
        else:
            ok = False
            reasons.append(f"健康分 {snap['health_score']} 低于最低要求 {compat.min_health_score}")
        if compat.max_snapshot_age_seconds is not None:
            if not snap["reported_at"]:
                ok = False
                reasons.append("快照缺少上报时间，视为过旧")
            else:
                age = (now_dt - _parse_dt(snap["reported_at"])).total_seconds()
                if age > compat.max_snapshot_age_seconds:
                    ok = False
                    reasons.append(
                        f"快照过旧：已滞后 {age:.0f} 秒，超过上限"
                        f" {compat.max_snapshot_age_seconds:.0f} 秒"
                    )
                else:
                    reasons.append(f"快照新鲜：滞后 {age:.0f} 秒")
        if flag_row is not None:
            if flag_row["quarantined"]:
                ok = False
                reasons.append(f"车辆已隔离：{flag_row['quarantine_reason']}")
            blocked = json.loads(flag_row["blocked_packages_json"])
            if package_id in blocked:
                ok = False
                reasons.append(f"车辆因包 {package_id} 回滚被封锁，不得被旧波次重新推进")
        return ok, reasons

    def _compute_metrics(
        self, conn: sqlite3.Connection, wave: sqlite3.Row, policy: RiskPolicy
    ) -> dict:
        wave_id = wave["wave_id"]
        members = conn.execute(
            "SELECT COUNT(*) AS c FROM wave_members WHERE wave_id=? AND included=1",
            (wave_id,),
        ).fetchone()["c"]
        counts = {
            r["status"]: r["c"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM commands WHERE wave_id=? AND kind=?"
                " GROUP BY status",
                (wave_id, CommandKind.INSTALL.value),
            ).fetchall()
        }
        issued = sum(counts.values())
        succeeded = counts.get(CommandStatus.SUCCEEDED.value, 0)
        failed = counts.get(CommandStatus.FAILED.value, 0)
        cancelled = counts.get(CommandStatus.CANCELLED.value, 0)
        received = succeeded + failed
        pending = issued - received - cancelled
        late = conn.execute(
            "SELECT COUNT(*) AS c FROM receipts WHERE wave_id=? AND late=1 AND duplicate=0",
            (wave_id,),
        ).fetchone()["c"]
        since = wave["started_at"] or wave["created_at"]
        incidents = conn.execute(
            "SELECT severity, COUNT(*) AS c FROM incidents"
            " WHERE received_at>=? AND vehicle_id IN"
            " (SELECT vehicle_id FROM wave_members WHERE wave_id=? AND included=1)"
            " GROUP BY severity",
            (since, wave_id),
        ).fetchall()
        critical = sum(
            r["c"] for r in incidents if r["severity"] == IncidentSeverity.CRITICAL.value
        )
        total_incidents = sum(r["c"] for r in incidents)
        avg_row = conn.execute(
            "SELECT AVG(score) AS a FROM ("
            "  SELECT COALESCE("
            "    (SELECT h.score FROM health_reports h WHERE h.vehicle_id=m.vehicle_id"
            "     ORDER BY h.reported_at DESC LIMIT 1),"
            "    (SELECT s.health_score FROM snapshots s WHERE s.vehicle_id=m.vehicle_id)"
            "  ) AS score FROM wave_members m WHERE m.wave_id=? AND m.included=1"
            ")",
            (wave_id,),
        ).fetchone()
        avg_health = avg_row["a"] if avg_row["a"] is not None else 1.0
        return {
            "members": members,
            "issued": issued,
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": cancelled,
            "received": received,
            "pending": pending,
            "late_receipts": late,
            "failure_rate": failed / issued if issued else 0.0,
            "allowed_failures": math.floor(members * policy.max_failure_rate),
            "critical_incidents": critical,
            "total_incidents": total_incidents,
            "avg_health_score": avg_health,
            "late_ratio": late / received if received else 0.0,
        }

    @staticmethod
    def _breaches(metrics: dict, policy: RiskPolicy) -> list[str]:
        breaches: list[str] = []
        if metrics["failed"] > metrics["allowed_failures"]:
            breaches.append(
                f"失败数 {metrics['failed']} 超过预算 {metrics['allowed_failures']}"
            )
        if metrics["critical_incidents"] > policy.max_critical_incidents:
            breaches.append(
                f"严重事件 {metrics['critical_incidents']} 起，超过上限"
                f" {policy.max_critical_incidents}"
            )
        if metrics["total_incidents"] > policy.max_incidents:
            breaches.append(
                f"事件总数 {metrics['total_incidents']} 起，超过上限 {policy.max_incidents}"
            )
        if metrics["members"] > 0 and metrics["avg_health_score"] < policy.min_avg_health_score:
            breaches.append(
                f"平均健康分 {metrics['avg_health_score']:.2f} 低于下限"
                f" {policy.min_avg_health_score}"
            )
        if metrics["late_ratio"] > policy.max_late_ratio:
            breaches.append(
                f"迟到回执占比 {metrics['late_ratio']:.2f} 超过上限 {policy.max_late_ratio}"
            )
        return breaches

    def _evaluate(self, conn: sqlite3.Connection, wave: sqlite3.Row) -> Optional[DecisionAction]:
        """风险评估：仅在 RUNNING 状态自动决策；决策追加记录，不可改写。"""
        if wave["state"] != RolloutState.RUNNING.value:
            return None
        policy = self._policy(conn, wave["policy_id"], wave["policy_version"])
        metrics = self._compute_metrics(conn, wave, policy)
        breaches = self._breaches(metrics, policy)
        if breaches and not wave["manual_override"]:
            reason = "风险预算超支：" + "；".join(breaches)
            self._record_decision(conn, wave, DecisionAction.PAUSE, policy, metrics, reason)
            self._transition(conn, wave, RolloutState.PAUSED, reason, actor="risk-engine")
            return DecisionAction.PAUSE
        if metrics["pending"] == 0:
            reason = (
                "全部安装命令已有最终回执且风险在预算内"
                if metrics["issued"] and not breaches
                else "全部安装命令已有最终回执（人工兜底下带超支完结）"
                if metrics["issued"]
                else "无可升级车辆，波次直接完结"
            )
            self._record_decision(conn, wave, DecisionAction.COMPLETE, policy, metrics, reason)
            self._transition(conn, wave, RolloutState.COMPLETED, reason, actor="risk-engine")
            return DecisionAction.COMPLETE
        if breaches:
            self._record_decision(
                conn, wave, DecisionAction.CONTINUE, policy, metrics,
                "人工兜底中，超支仅记录不自动暂停：" + "；".join(breaches),
            )
            return DecisionAction.CONTINUE
        self._record_decision(
            conn, wave, DecisionAction.CONTINUE, policy, metrics,
            f"风险在预算内，等待 {metrics['pending']} 条命令的回执",
        )
        return DecisionAction.CONTINUE

    def _record_decision(
        self,
        conn: sqlite3.Connection,
        wave: sqlite3.Row,
        action: DecisionAction,
        policy: RiskPolicy,
        metrics: dict,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT INTO decisions (wave_id, action, policy_id, policy_version,"
            " metrics_json, reason, at) VALUES (?,?,?,?,?,?,?)",
            (
                wave["wave_id"],
                action.value,
                policy.policy_id,
                policy.version,
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                reason,
                self._now_iso(),
            ),
        )

    def _recovery_conditions(
        self, conn: sqlite3.Connection, wave: sqlite3.Row
    ) -> list[RecoveryCondition]:
        policy = self._policy(conn, wave["policy_id"], wave["policy_version"])
        m = self._compute_metrics(conn, wave, policy)
        return [
            RecoveryCondition(
                name="失败预算",
                required=f"失败数 ≤ {m['allowed_failures']}",
                current=str(m["failed"]),
                met=m["failed"] <= m["allowed_failures"],
            ),
            RecoveryCondition(
                name="严重事件",
                required=f"≤ {policy.max_critical_incidents} 起",
                current=f"{m['critical_incidents']} 起",
                met=m["critical_incidents"] <= policy.max_critical_incidents,
            ),
            RecoveryCondition(
                name="事件总数",
                required=f"≤ {policy.max_incidents} 起",
                current=f"{m['total_incidents']} 起",
                met=m["total_incidents"] <= policy.max_incidents,
            ),
            RecoveryCondition(
                name="平均健康分",
                required=f"≥ {policy.min_avg_health_score}",
                current=f"{m['avg_health_score']:.2f}",
                met=m["members"] == 0 or m["avg_health_score"] >= policy.min_avg_health_score,
            ),
            RecoveryCondition(
                name="迟到回执占比",
                required=f"≤ {policy.max_late_ratio}",
                current=f"{m['late_ratio']:.2f}",
                met=m["late_ratio"] <= policy.max_late_ratio,
            ),
        ]

    def _wave_detail(self, conn: sqlite3.Connection, wave: sqlite3.Row) -> WaveDetail:
        members = conn.execute(
            "SELECT COUNT(*) AS c FROM wave_members WHERE wave_id=? AND included=1",
            (wave["wave_id"],),
        ).fetchone()["c"]
        issued = conn.execute(
            "SELECT COUNT(*) AS c FROM commands WHERE wave_id=? AND kind=?",
            (wave["wave_id"], CommandKind.INSTALL.value),
        ).fetchone()["c"]
        received = conn.execute(
            "SELECT COUNT(*) AS c FROM receipts WHERE wave_id=? AND duplicate=0",
            (wave["wave_id"],),
        ).fetchone()["c"]
        return WaveDetail(
            wave_id=wave["wave_id"],
            package_id=wave["package_id"],
            seq=wave["seq"],
            target_percent=wave["target_percent"],
            state=wave["state"],
            policy_id=wave["policy_id"],
            policy_version=wave["policy_version"],
            manual_override=bool(wave["manual_override"]),
            created_at=wave["created_at"],
            started_at=wave["started_at"],
            updated_at=wave["updated_at"],
            members_included=members,
            commands_issued=issued,
            receipts_received=received,
        )

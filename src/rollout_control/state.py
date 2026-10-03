"""服务内部的可变状态记录。

所有记录都只能由事件应用（service._apply）修改；
每个记录提供 to_dict / from_dict 以支持事件持久化与重启回放。
"""

from dataclasses import dataclass, field

from .contracts import (
    CommandKind,
    CommandStatus,
    PlanState,
    RolloutState,
    VehicleSnapshot,
    VehicleStatus,
)


@dataclass
class Assignment:
    """一辆车在某波次中的入组记录。reasons 为入组理由（审计可见）。"""

    vehicle_id: str
    epoch: int
    reasons: list[str]
    status: VehicleStatus = VehicleStatus.SCHEDULED
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "epoch": self.epoch,
            "reasons": list(self.reasons),
            "status": self.status.value,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Assignment":
        return cls(
            vehicle_id=data["vehicle_id"],
            epoch=data["epoch"],
            reasons=list(data["reasons"]),
            status=VehicleStatus(data["status"]),
            attempts=data.get("attempts", 0),
        )


@dataclass
class CommandRecord:
    """一条下发给单车的安装/回滚命令。

    command_id 由 (计划, 波次, 车辆, 纪元, 尝试序号, 类型) 确定性派生，
    同一命令重复下发只会得到同一条记录（幂等）。
    """

    command_id: str
    plan_id: str
    wave_id: str
    vehicle_id: str
    epoch: int
    attempt: int
    kind: CommandKind
    status: CommandStatus = CommandStatus.SENT
    error_code: str = ""
    created_at: float = 0.0
    closed_by_receipt: str = ""

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "plan_id": self.plan_id,
            "wave_id": self.wave_id,
            "vehicle_id": self.vehicle_id,
            "epoch": self.epoch,
            "attempt": self.attempt,
            "kind": self.kind.value,
            "status": self.status.value,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "closed_by_receipt": self.closed_by_receipt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CommandRecord":
        return cls(
            command_id=data["command_id"],
            plan_id=data["plan_id"],
            wave_id=data["wave_id"],
            vehicle_id=data["vehicle_id"],
            epoch=data["epoch"],
            attempt=data["attempt"],
            kind=CommandKind(data["kind"]),
            status=CommandStatus(data["status"]),
            error_code=data.get("error_code", ""),
            created_at=data.get("created_at", 0.0),
            closed_by_receipt=data.get("closed_by_receipt", ""),
        )


@dataclass
class Transition:
    from_state: str
    to_state: str
    at: float
    reason: str
    decision_id: str = ""

    def to_dict(self) -> dict:
        return {
            "from": self.from_state,
            "to": self.to_state,
            "at": self.at,
            "reason": self.reason,
            "decision_id": self.decision_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transition":
        return cls(
            from_state=data["from"],
            to_state=data["to"],
            at=data["at"],
            reason=data["reason"],
            decision_id=data.get("decision_id", ""),
        )


@dataclass
class WaveRecord:
    wave_id: str
    ordinal: int
    state: RolloutState = RolloutState.PENDING
    cohort: dict[str, Assignment] = field(default_factory=dict)
    transitions: list[Transition] = field(default_factory=list)
    started_at: float = 0.0
    closed_at: float = 0.0
    final_decision_id: str = ""

    def to_dict(self) -> dict:
        return {
            "wave_id": self.wave_id,
            "ordinal": self.ordinal,
            "state": self.state.value,
            "cohort": {vid: a.to_dict() for vid, a in self.cohort.items()},
            "transitions": [t.to_dict() for t in self.transitions],
            "started_at": self.started_at,
            "closed_at": self.closed_at,
            "final_decision_id": self.final_decision_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WaveRecord":
        return cls(
            wave_id=data["wave_id"],
            ordinal=data["ordinal"],
            state=RolloutState(data["state"]),
            cohort={vid: Assignment.from_dict(a) for vid, a in data["cohort"].items()},
            transitions=[Transition.from_dict(t) for t in data.get("transitions", [])],
            started_at=data.get("started_at", 0.0),
            closed_at=data.get("closed_at", 0.0),
            final_decision_id=data.get("final_decision_id", ""),
        )


@dataclass
class DecisionRecord:
    """一次风险判定。记录判定所用的规则版本与输入指标，落库后不可变。"""

    decision_id: str
    plan_id: str
    wave_id: str
    policy_version: str
    verdict: str
    metrics: dict
    reasons: list[str]
    created_at: float
    seq: int = 0

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "plan_id": self.plan_id,
            "wave_id": self.wave_id,
            "policy_version": self.policy_version,
            "verdict": self.verdict,
            "metrics": dict(self.metrics),
            "reasons": list(self.reasons),
            "created_at": self.created_at,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DecisionRecord":
        return cls(
            decision_id=data["decision_id"],
            plan_id=data["plan_id"],
            wave_id=data["wave_id"],
            policy_version=data["policy_version"],
            verdict=data["verdict"],
            metrics=dict(data["metrics"]),
            reasons=list(data["reasons"]),
            created_at=data["created_at"],
            seq=data.get("seq", 0),
        )


@dataclass
class PlanRecord:
    plan_id: str
    package_id: str
    policy_version: str
    created_by: str
    auto_promote: bool = True
    state: PlanState = PlanState.DRAFT
    approved_by: str = ""
    waves: list[WaveRecord] = field(default_factory=list)
    exclusions: dict[str, list[str]] = field(default_factory=dict)
    transitions: list[Transition] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    created_at: float = 0.0

    def active_wave(self) -> WaveRecord | None:
        for wave in self.waves:
            if wave.state in (RolloutState.RUNNING, RolloutState.PAUSED):
                return wave
        return None

    def next_pending_wave(self) -> WaveRecord | None:
        for wave in self.waves:
            if wave.state == RolloutState.PENDING:
                return wave
        return None


@dataclass
class VehicleRecord:
    snapshot: VehicleSnapshot
    epoch: int = 0


@dataclass
class ReceiptRecord:
    receipt_id: str
    command_id: str
    vehicle_id: str
    success: bool
    disposition: str
    error_code: str = ""
    occurred_at: float = 0.0
    received_at: float = 0.0
    duplicate_count: int = 0  # 同一幂等键被重复上报的次数

    def to_dict(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "vehicle_id": self.vehicle_id,
            "success": self.success,
            "disposition": self.disposition,
            "error_code": self.error_code,
            "occurred_at": self.occurred_at,
            "received_at": self.received_at,
            "duplicate_count": self.duplicate_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReceiptRecord":
        return cls(
            receipt_id=data["receipt_id"],
            command_id=data["command_id"],
            vehicle_id=data["vehicle_id"],
            success=data["success"],
            disposition=data["disposition"],
            error_code=data.get("error_code", ""),
            occurred_at=data.get("occurred_at", 0.0),
            received_at=data.get("received_at", 0.0),
            duplicate_count=data.get("duplicate_count", 0),
        )


@dataclass
class IncidentRecord:
    incident_id: str
    severity: str
    summary: str
    vehicle_id: str = ""
    hardware_batch: str = ""
    reported_at: float = 0.0
    resolved: bool = False
    resolved_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "severity": self.severity,
            "summary": self.summary,
            "vehicle_id": self.vehicle_id,
            "hardware_batch": self.hardware_batch,
            "reported_at": self.reported_at,
            "resolved": self.resolved,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IncidentRecord":
        return cls(
            incident_id=data["incident_id"],
            severity=data["severity"],
            summary=data["summary"],
            vehicle_id=data.get("vehicle_id", ""),
            hardware_batch=data.get("hardware_batch", ""),
            reported_at=data.get("reported_at", 0.0),
            resolved=data.get("resolved", False),
            resolved_at=data.get("resolved_at", 0.0),
        )


@dataclass
class QuarantineRecord:
    hardware_batch: str
    plan_id: str
    reason: str
    decision_id: str
    quarantined_at: float
    cleared: bool = False
    cleared_at: float = 0.0
    cleared_by: str = ""

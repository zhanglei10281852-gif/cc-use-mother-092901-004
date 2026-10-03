"""灰度发布领域的数据契约。

在初版（车辆快照 / 发布波次 / 状态枚举）之上扩展出软件包、兼容条件、
风险策略、回执、人工事件与运维视图等完整契约，供灰度发布控制服务使用。
所有枚举值保持向后兼容，旧的四元 VehicleSnapshot / RolloutWave 构造方式不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RolloutState(StrEnum):
    """发布波次状态机。"""

    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    QUARANTINED = "quarantined"
    # 兼容初版契约
    PENDING = "pending"
    DRAFT = "draft"


class CommandKind(StrEnum):
    INSTALL = "install"
    ROLLBACK = "rollback"


class CommandStatus(StrEnum):
    ISSUED = "issued"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReceiptStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class IncidentSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionAction(StrEnum):
    """风险引擎对波次的自动决策。"""

    CONTINUE = "continue"
    COMPLETE = "complete"
    PAUSE = "pause"


TERMINAL_WAVE_STATES = frozenset(
    {RolloutState.ROLLED_BACK, RolloutState.COMPLETED, RolloutState.QUARANTINED}
)
FINAL_COMMAND_STATUSES = frozenset(
    {CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.CANCELLED}
)


@dataclass(frozen=True)
class VehicleSnapshot:
    """车辆上报的在线状态 / 硬件批次 / 版本 / 电量 / 健康分快照。

    前四个位置参数与初版契约一致，其余字段带默认值。
    reported_at 为空表示上报时间未知，新鲜度校验会将其视为过旧。
    """

    vehicle_id: str
    hardware_batch: str
    software_version: str
    battery_percent: int
    model: str = ""
    online: bool = True
    health_score: float = 1.0
    reported_at: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.battery_percent <= 100:
            raise ValueError("电量百分比必须位于零到一百之间")
        if not 0.0 <= self.health_score <= 1.0:
            raise ValueError("健康分必须位于零到一之间")


@dataclass(frozen=True)
class Compatibility:
    """软件包的兼容条件；空集合表示不限制。"""

    allowed_hardware_batches: tuple[str, ...] = ()
    allowed_from_versions: tuple[str, ...] = ()
    min_battery_percent: int = 0
    require_online: bool = False
    min_health_score: float = 0.0
    max_snapshot_age_seconds: float | None = None


@dataclass(frozen=True)
class SoftwarePackage:
    package_id: str
    model: str
    version: str
    compatibility: Compatibility = Compatibility()


@dataclass(frozen=True)
class RiskPolicy:
    """版本化的风险阈值。换版必须递增 version，禁止原地修改。"""

    policy_id: str
    version: int
    max_failure_rate: float = 0.05
    max_critical_incidents: int = 0
    max_incidents: int = 100
    min_avg_health_score: float = 0.0
    receipt_timeout_seconds: float = 3600.0
    max_late_ratio: float = 1.0

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("策略版本号必须为正整数")
        if not 0.0 <= self.max_failure_rate <= 1.0:
            raise ValueError("失败率阈值必须位于零到一之间")
        if not 0.0 <= self.min_avg_health_score <= 1.0:
            raise ValueError("平均健康分阈值必须位于零到一之间")
        if self.receipt_timeout_seconds <= 0:
            raise ValueError("回执超时时间必须为正数")


@dataclass(frozen=True)
class RolloutWave:
    """初版契约保留：一小批车辆的发布波次视图。"""

    wave_id: str
    package_id: str
    vehicles: tuple[VehicleSnapshot, ...]
    state: RolloutState = RolloutState.PENDING

    def __post_init__(self) -> None:
        if not self.vehicles:
            raise ValueError("发布波次必须包含车辆")


@dataclass(frozen=True)
class MemberRationale:
    """波次成员（含落选车辆）的入组 / 排除理由。"""

    wave_id: str
    vehicle_id: str
    included: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TransitionRecord:
    transition_id: int
    wave_id: str
    from_state: str
    to_state: str
    reason: str
    actor: str
    at: str


@dataclass(frozen=True)
class DecisionRecord:
    """一次风险评估决策，追加式记录，完成后不可被规则换版改写。"""

    decision_id: int
    wave_id: str
    action: str
    policy_id: str
    policy_version: int
    metrics: dict
    reason: str
    at: str


@dataclass(frozen=True)
class RecoveryCondition:
    name: str
    required: str
    current: str
    met: bool


@dataclass(frozen=True)
class RiskBudget:
    """波次实时风险预算视图。"""

    wave_id: str
    policy_id: str
    policy_version: int
    members: int
    commands_issued: int
    receipts_received: int
    succeeded: int
    failed: int
    cancelled: int
    pending: int
    late_receipts: int
    failure_rate: float
    allowed_failures: int
    failures_remaining: int
    critical_incidents: int
    max_critical_incidents: int
    total_incidents: int
    max_incidents: int
    avg_health_score: float
    min_avg_health_score: float
    late_ratio: float
    max_late_ratio: float
    within_budget: bool
    breaches: tuple[str, ...]


@dataclass(frozen=True)
class WaveDetail:
    wave_id: str
    package_id: str
    seq: int
    target_percent: float
    state: str
    policy_id: str
    policy_version: int
    manual_override: bool
    created_at: str
    started_at: str
    updated_at: str
    members_included: int
    commands_issued: int
    receipts_received: int

"""灰度发布计划所需的数据契约。

本模块是对外（API / CLI / 测试）共享的不可变契约对象；
服务内部的可变状态见 state.py。
"""

from dataclasses import dataclass, field
from enum import StrEnum


class RolloutState(StrEnum):
    """波次状态机。ROLLED_BACK 与 COMPLETED 为终态。"""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"


class PlanState(StrEnum):
    """发布计划状态机。"""

    DRAFT = "draft"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


class VehicleStatus(StrEnum):
    """单车在某计划内的安装进度。"""

    SCHEDULED = "scheduled"        # 已入组，等待所属波次启动
    COMMAND_SENT = "command_sent"  # 安装命令已下发，等待回执
    INSTALLED = "installed"        # 安装成功
    FAILED = "failed"              # 最近一次尝试失败（可重试）
    QUARANTINED = "quarantined"    # 因批次隔离被移出本次发布
    ROLLING_BACK = "rolling_back"  # 已下发回滚命令
    ROLLED_BACK = "rolled_back"    # 回滚成功
    EXCLUDED = "excluded"          # 被排除（纪元围栏 / 回滚时未安装）


class CommandKind(StrEnum):
    INSTALL = "install"
    ROLLBACK = "rollback"


class CommandStatus(StrEnum):
    SENT = "sent"            # 已下发，等待回执
    SUCCEEDED = "succeeded"  # 收到成功回执（终态）
    FAILED = "failed"        # 收到失败回执（终态，可重试产生新命令）
    SUPERSEDED = "superseded"  # 被重试 / 回滚 / 隔离取代（终态）


class ReceiptDisposition(StrEnum):
    """回执受理结果。任何回执都会被记录，但只有 APPLIED 会改变状态。

    同一 receipt_id 的重复上报直接返回首次受理结果（不计为新回执）。
    """

    APPLIED = "applied"                        # 正常受理
    DUPLICATE_COMMAND = "duplicate_command"    # 命令已有终态回执的重复回报
    SUPERSEDED_COMMAND = "superseded_command"  # 命令已被更新的尝试取代（迟到）
    STALE_EPOCH = "stale_epoch"                # 纪元过旧（回滚/隔离后的迟到回执）
    LATE_WAVE_CLOSED = "late_wave_closed"      # 波次已终结后的迟到回执
    UNKNOWN_COMMAND = "unknown_command"        # 命令不存在


class Verdict(StrEnum):
    """风险判定结论。"""

    CONTINUE = "continue"
    PAUSE = "pause"
    ROLLBACK = "rollback"
    QUARANTINE_BATCH = "quarantine_batch"
    INSUFFICIENT_DATA = "insufficient_data"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class VehicleSnapshot:
    vehicle_id: str
    hardware_batch: str
    software_version: str
    battery_percent: int
    online: bool = True
    model: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.battery_percent <= 100:
            raise ValueError("电量百分比必须位于零到一百之间")


@dataclass(frozen=True)
class RolloutWave:
    wave_id: str
    package_id: str
    vehicles: tuple[VehicleSnapshot, ...]
    state: RolloutState = RolloutState.PENDING

    def __post_init__(self) -> None:
        if not self.vehicles:
            raise ValueError("发布波次必须包含车辆")


@dataclass(frozen=True)
class Compatibility:
    """软件包对车辆的兼容条件。

    空集合表示不限制该维度。
    """

    hardware_batches: frozenset[str] = frozenset()
    source_versions: frozenset[str] = frozenset()
    min_battery_percent: int = 0
    require_online: bool = True
    models: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 0 <= self.min_battery_percent <= 100:
            raise ValueError("最低电量门槛必须位于零到一百之间")


@dataclass(frozen=True)
class SoftwarePackage:
    package_id: str
    version: str
    compatibility: Compatibility = field(default_factory=Compatibility)
    description: str = ""


@dataclass(frozen=True)
class RiskPolicy:
    """风险阈值。按版本登记，登记后不可变；

    计划创建时钉住某一版本，之后的规则调整只影响显式迁移后的新判定，
    已完成的判定永远保留当时的版本与输入。
    """

    version: str
    pause_failure_rate: float       # 风险率达到该值 -> 暂停
    rollback_failure_rate: float    # 风险率达到该值 -> 回滚
    batch_quarantine_rate: float    # 单硬件批次失败率达到该值 -> 隔离该批次
    min_batch_sample: int = 2       # 批次判定所需的最少终态回执数
    min_wave_sample: int = 1        # 波次判定所需的最少终态回执数
    unhealthy_score_below: int = 60  # 健康分低于该值视为风险当量
    pause_incident_count: int = 1    # 未解决严重事件达到该数 -> 暂停
    rollback_incident_count: int = 2  # 未解决严重事件达到该数 -> 回滚

    def __post_init__(self) -> None:
        rates = (self.pause_failure_rate, self.rollback_failure_rate, self.batch_quarantine_rate)
        if any(not 0.0 <= r <= 1.0 for r in rates):
            raise ValueError("失败率阈值必须位于零到一之间")
        if not self.pause_failure_rate < self.rollback_failure_rate:
            raise ValueError("暂停阈值必须小于回滚阈值")
        if self.min_batch_sample < 1 or self.min_wave_sample < 1:
            raise ValueError("样本数门槛必须至少为一")
        if not 0 <= self.unhealthy_score_below <= 100:
            raise ValueError("健康分阈值必须位于零到一百之间")
        if self.pause_incident_count < 1 or self.rollback_incident_count < self.pause_incident_count:
            raise ValueError("事件数门槛不合法")


@dataclass(frozen=True)
class InstallReceipt:
    """车端安装回执。receipt_id 为幂等键，由上报方生成。"""

    receipt_id: str
    command_id: str
    vehicle_id: str
    success: bool
    error_code: str = ""
    occurred_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.receipt_id:
            raise ValueError("回执必须携带幂等键 receipt_id")
        if not self.command_id or not self.vehicle_id:
            raise ValueError("回执必须关联命令与车辆")


@dataclass(frozen=True)
class HealthReport:
    vehicle_id: str
    health_score: int
    reported_at: float
    fault_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.health_score <= 100:
            raise ValueError("健康分必须位于零到一百之间")


@dataclass(frozen=True)
class IncidentReport:
    """人工事件报告。vehicle_id 与 hardware_batch 至少提供一个。"""

    incident_id: str
    severity: Severity
    summary: str
    vehicle_id: str = ""
    hardware_batch: str = ""
    reported_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.incident_id:
            raise ValueError("事件必须携带 incident_id")
        if not self.vehicle_id and not self.hardware_batch:
            raise ValueError("事件必须关联车辆或硬件批次")

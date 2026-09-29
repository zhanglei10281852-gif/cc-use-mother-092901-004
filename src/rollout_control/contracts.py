"""灰度发布计划所需的数据契约。"""

from dataclasses import dataclass
from enum import StrEnum


class RolloutState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"


@dataclass(frozen=True)
class VehicleSnapshot:
    vehicle_id: str
    hardware_batch: str
    software_version: str
    battery_percent: int

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

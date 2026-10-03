"""测试共享辅助：构建服务、车辆与常用流程。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from rollout_control import (  # noqa: E402
    Compatibility,
    RiskPolicy,
    RolloutControlService,
    SoftwarePackage,
    VehicleSnapshot,
)


def make_clock(start: float = 1_700_000_000.0):
    """可手动推进的时钟，保证测试时间确定。"""
    state = {"now": start}

    def clock() -> float:
        state["now"] += 1.0
        return state["now"]

    return clock


def make_service(tmpdir: str | None = None, clock=None) -> tuple[RolloutControlService, str]:
    data_dir = tmpdir or tempfile.mkdtemp(prefix="rollout-test-")
    service = RolloutControlService(data_dir, clock=clock or make_clock(), fsync=False)
    return service, data_dir


def register_defaults(service: RolloutControlService, vehicle_count: int = 10,
                      batches: tuple[str, ...] = ("hw-a", "hw-b")) -> None:
    service.register_package(SoftwarePackage(
        package_id="pkg-1",
        version="sw-2.0",
        compatibility=Compatibility(
            hardware_batches=frozenset(batches),
            source_versions=frozenset({"sw-1.0"}),
            min_battery_percent=30,
        ),
    ))
    service.register_policy(RiskPolicy(
        version="v1",
        pause_failure_rate=0.2,
        rollback_failure_rate=0.5,
        batch_quarantine_rate=0.5,
        min_batch_sample=2,
    ))
    for i in range(vehicle_count):
        service.register_vehicle(VehicleSnapshot(
            f"V-{i:03d}", batches[i % len(batches)], "sw-1.0", 80))


def vehicle_ids(service: RolloutControlService, plan_id: str, wave_ordinal: int) -> list[str]:
    plan = service.get_plan(plan_id)
    wave = plan["waves"][wave_ordinal - 1]
    return [c["vehicle_id"] for c in wave["commands"]]


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.data_dir = make_service()

    def reopen(self) -> RolloutControlService:
        """在同一数据目录上重建服务，模拟服务重启。"""
        self.service = RolloutControlService(self.data_dir, clock=make_clock(), fsync=False)
        return self.service

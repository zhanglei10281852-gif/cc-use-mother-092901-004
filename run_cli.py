"""命令行冒烟：完整走一遍灰度发布流程并打印波次报告。

用法：python run_cli.py [数据目录]
默认使用临时目录，传入目录可观察事件日志 events.jsonl。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from rollout_control import (
    Compatibility,
    InstallReceipt,
    RiskPolicy,
    RolloutControlService,
    SoftwarePackage,
    VehicleSnapshot,
)


def main() -> None:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="rollout-demo-")
    service = RolloutControlService(data_dir)

    service.register_package(SoftwarePackage(
        package_id="pkg-adas-7",
        version="adas-7.0",
        compatibility=Compatibility(
            hardware_batches=frozenset({"hw-a", "hw-b"}),
            source_versions=frozenset({"adas-6.5"}),
            min_battery_percent=30,
        ),
        description="驾驶辅助 7.0",
    ))
    service.register_policy(RiskPolicy(
        version="v1",
        pause_failure_rate=0.2,
        rollback_failure_rate=0.5,
        batch_quarantine_rate=0.5,
        min_batch_sample=2,
    ))
    for i in range(10):
        batch = "hw-a" if i % 2 == 0 else "hw-b"
        service.register_vehicle(VehicleSnapshot(
            f"VIN-{i:03d}", batch, "adas-6.5", 80, model="sedan-x"))
    # 一辆离线车与一辆低电量车应被排除在计划外。
    service.register_vehicle(VehicleSnapshot("VIN-OFF", "hw-a", "adas-6.5", 80, online=False))
    service.register_vehicle(VehicleSnapshot("VIN-LOW", "hw-b", "adas-6.5", 10))

    plan_id = service.create_plan("pkg-adas-7", "v1", [0.5, 1.0], created_by="release-mgr")
    service.approve_plan(plan_id, approver="qa-lead")

    wave1 = service.start_next_wave(plan_id)
    commands = {c["vehicle_id"]: c["command_id"] for c in wave1["commands"]}
    # 小流量波次：一车成功，一车失败并重复上报，触发暂停。
    service.ingest_receipt(InstallReceipt("rc-1", commands["VIN-000"], "VIN-000", True))
    fail_cmd = commands["VIN-001"]
    service.ingest_receipt(InstallReceipt("rc-2", fail_cmd, "VIN-001", False, "E_FLASH"))
    service.ingest_receipt(InstallReceipt("rc-2", fail_cmd, "VIN-001", False, "E_FLASH"))  # 重复回执

    report = service.wave_report(plan_id, f"{plan_id}-w1")
    print(json.dumps({
        "plan_id": plan_id,
        "wave_state": report["state"],
        "cohort": {c["vehicle_id"]: c["status"] for c in report["cohort"]},
        "risk_budget": report["risk_budget"],
        "recovery_conditions": report["recovery_conditions"],
        "excluded_from_plan": service.get_plan(plan_id)["exclusions"],
        "data_dir": data_dir,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

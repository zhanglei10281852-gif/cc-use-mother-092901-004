"""灰度发布控制服务端到端冒烟演示。

流程：登记策略/软件包/车辆快照 → 审批 → 小流量波次 → 回执驱动完结
→ 第二波 → 人工事件触发暂停 → 恢复条件 → 回滚 → 车辆封锁视图。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from rollout_control import (
    Compatibility,
    RiskPolicy,
    RolloutControlService,
    SoftwarePackage,
    VehicleSnapshot,
)


def show(title, payload):
    print(f"== {title} ==")
    print(json.dumps(payload, ensure_ascii=False, default=lambda o: getattr(o, "__dict__", str(o)), indent=2))


with tempfile.TemporaryDirectory() as tmp:
    svc = RolloutControlService(str(Path(tmp) / "demo.db"))

    svc.register_policy(RiskPolicy(policy_id="p", version=1, max_failure_rate=0.34))
    svc.register_package(SoftwarePackage(
        package_id="pkg-adas-2.0",
        model="sedan-x",
        version="2.0.0",
        compatibility=Compatibility(
            allowed_hardware_batches=("hw-a", "hw-b"),
            allowed_from_versions=("sw-1.9",),
            min_battery_percent=40,
            require_online=True,
            max_snapshot_age_seconds=3600,
        ),
    ))
    for i in range(1, 7):
        svc.register_snapshot(VehicleSnapshot(
            vehicle_id=f"VIN-{i}", hardware_batch="hw-a" if i % 2 else "hw-b",
            software_version="sw-1.9", battery_percent=80, model="sedan-x",
        ))

    # 第一波：30% 小流量，审批后启动
    svc.create_wave("W1", "pkg-adas-2.0", seq=1, target_percent=0.3, policy_id="p")
    svc.approve_wave("W1", actor="release-manager")
    svc.start_wave("W1", actor="release-manager")
    for cmd in svc.list_commands("W1"):
        svc.submit_receipt(f"rcpt-{cmd['command_id']}", cmd["command_id"], "success")
    show("W1 完结后的风险预算", svc.get_risk_budget("W1"))

    # 第二波：100%，一起严重人工事件触发自动暂停
    svc.create_wave("W2", "pkg-adas-2.0", seq=2, target_percent=1.0, policy_id="p")
    svc.approve_wave("W2", actor="release-manager")
    svc.start_wave("W2", actor="release-manager")
    svc.report_incident("INC-1", "VIN-4", "critical", "车主报障：辅助驾驶异常退出")
    show("W2 自动暂停后的恢复条件", svc.get_recovery_conditions("W2"))

    # 回滚第二波：成员车辆被封锁，不再被旧波次推进
    svc.rollback_wave("W2", reason="严重事件待调查，先行回滚")
    show("VIN-4 车辆视图", svc.get_vehicle("VIN-4"))
    show("W2 状态迁移", svc.get_transitions("W2"))

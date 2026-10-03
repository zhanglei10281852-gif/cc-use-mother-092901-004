"""主流程：登记 → 审批 → 小流量逐级放量 → 完成。"""

import unittest

from helpers import ServiceTestCase, register_defaults
from rollout_control import (
    ConflictError,
    InstallReceipt,
    PlanState,
    RolloutState,
    VehicleSnapshot,
    VehicleStatus,
)


class HappyPathTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=10)

    def _receipt(self, n: int, command_id: str, vehicle_id: str, ok: bool = True):
        return self.service.ingest_receipt(
            InstallReceipt(f"rc-{n}", command_id, vehicle_id, ok))

    def test_full_rollout_two_waves(self):
        plan_id = self.service.create_plan("pkg-1", "v1", [0.2, 1.0],
                                           created_by="release-mgr")
        plan = self.service.get_plan(plan_id)
        self.assertEqual(plan["state"], PlanState.DRAFT.value)
        self.assertEqual([w["cohort_size"] for w in plan["waves"]], [2, 8])

        # 未审批不能放量
        with self.assertRaises(ConflictError):
            self.service.start_next_wave(plan_id)
        self.service.approve_plan(plan_id, approver="qa")

        wave1 = self.service.start_next_wave(plan_id)
        self.assertEqual(wave1["state"], RolloutState.RUNNING.value)
        self.assertEqual(len(wave1["commands"]), 2)
        cmd_by_vehicle = {c["vehicle_id"]: c["command_id"] for c in wave1["commands"]}

        # 第一波全部成功 -> 自动进入第二波
        for i, (vid, cid) in enumerate(sorted(cmd_by_vehicle.items())):
            self._receipt(i, cid, vid)
        plan = self.service.get_plan(plan_id)
        self.assertEqual(plan["waves"][0]["state"], RolloutState.COMPLETED.value)
        self.assertEqual(plan["waves"][1]["state"], RolloutState.RUNNING.value)
        self.assertEqual(len(plan["waves"][1]["commands"]), 8)

        # 第二波全部成功 -> 计划完成
        wave2_cmds = plan["waves"][1]["commands"]
        for i, cmd in enumerate(sorted(wave2_cmds, key=lambda c: c["command_id"])):
            self._receipt(100 + i, cmd["command_id"], cmd["vehicle_id"])
        plan = self.service.get_plan(plan_id)
        self.assertEqual(plan["state"], PlanState.COMPLETED.value)

        # 状态迁移轨迹完整：draft -> approved -> running -> completed
        states = [t["to"] for t in plan["transitions"]]
        self.assertEqual(states, ["approved", "running", "completed"])

    def test_command_ids_are_deterministic_and_idempotent(self):
        plan_id = self.service.create_plan("pkg-1", "v1", [0.2, 1.0], created_by="m")
        self.service.approve_plan(plan_id, approver="qa")
        wave1 = self.service.start_next_wave(plan_id)
        ids = [c["command_id"] for c in wave1["commands"]]
        self.assertEqual(len(ids), len(set(ids)))
        # 重复调用 start_next_wave 不会产生新命令
        again = self.service.start_next_wave(plan_id)
        self.assertEqual([c["command_id"] for c in again["commands"]], ids)

    def test_inclusion_reasons_are_recorded(self):
        plan_id = self.service.create_plan("pkg-1", "v1", [0.5, 1.0], created_by="m")
        report = self.service.wave_report(plan_id, f"{plan_id}-w1")
        self.assertEqual(len(report["cohort"]), 5)
        for member in report["cohort"]:
            self.assertEqual(member["status"], VehicleStatus.SCHEDULED.value)
            joined = "；".join(member["reasons"])
            self.assertIn("在兼容列表", joined)
            self.assertIn("电量", joined)
            self.assertIn("在线", joined)

    def test_ineligible_vehicles_are_excluded_with_reasons(self):
        self.service.register_vehicle(
            VehicleSnapshot("V-OFF", "hw-a", "sw-1.0", 80, online=False))
        self.service.register_vehicle(
            VehicleSnapshot("V-LOWBAT", "hw-a", "sw-1.0", 10))
        self.service.register_vehicle(
            VehicleSnapshot("V-DONE", "hw-a", "sw-2.0", 80))
        self.service.register_vehicle(
            VehicleSnapshot("V-WRONGHW", "hw-z", "sw-1.0", 80))
        plan_id = self.service.create_plan("pkg-1", "v1", [1.0], created_by="m")
        plan = self.service.get_plan(plan_id)
        excluded = {e["vehicle_id"]: "；".join(e["reasons"]) for e in plan["exclusions"]}
        self.assertIn("车辆在线", excluded["V-OFF"])
        self.assertIn("电量", excluded["V-LOWBAT"])
        self.assertIn("目标版本", excluded["V-DONE"])
        self.assertIn("兼容列表", excluded["V-WRONGHW"])
        # 10 辆合格车全部入组
        self.assertEqual(plan["waves"][0]["cohort_size"], 10)


if __name__ == "__main__":
    unittest.main()

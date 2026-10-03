"""回滚围栏：被回滚的车辆不得被旧波次重新推进。"""

import unittest

from helpers import ServiceTestCase, register_defaults
from rollout_control import (
    Compatibility,
    ConflictError,
    InstallReceipt,
    PlanState,
    ReceiptDisposition,
    SoftwarePackage,
    VehicleSnapshot,
)


class RollbackFenceTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=4)
        self.plan_id = self.service.create_plan("pkg-1", "v1", [1.0], created_by="m")
        self.service.approve_plan(self.plan_id, "qa")
        wave = self.service.start_next_wave(self.plan_id)
        self.wave_id = wave["wave_id"]
        self.commands = {c["vehicle_id"]: c["command_id"] for c in wave["commands"]}
        # 两车成功、两车未回报时触发人工回滚
        vids = sorted(self.commands)
        self.installed = vids[:2]
        self.pending = vids[2:]
        for i, vid in enumerate(self.installed):
            self.service.ingest_receipt(
                InstallReceipt(f"ok-{i}", self.commands[vid], vid, True))
        self.service.rollback_plan(self.plan_id, "发现严重缺陷", operator="ops-1")

    def test_rolled_back_plan_cannot_be_advanced(self):
        with self.assertRaises(ConflictError):
            self.service.start_next_wave(self.plan_id)
        with self.assertRaises(ConflictError):
            self.service.resume_plan(self.plan_id, operator="ops-1")
        self.assertIsNone(self.service.evaluate_plan(self.plan_id))

    def test_stale_receipts_from_old_epoch_are_fenced(self):
        # 已安装车辆纪元已提升，旧安装命令的迟到回执不再生效
        vid = self.installed[0]
        late = self.service.ingest_receipt(
            InstallReceipt("late-1", self.commands[vid], vid, True))
        self.assertEqual(late.disposition, ReceiptDisposition.STALE_EPOCH.value)
        # 未安装车辆被排除，其在途命令已被取代
        vid2 = self.pending[0]
        late2 = self.service.ingest_receipt(
            InstallReceipt("late-2", self.commands[vid2], vid2, True))
        self.assertEqual(late2.disposition,
                         ReceiptDisposition.SUPERSEDED_COMMAND.value)
        vehicle = self.service.get_vehicle(vid2)
        self.assertEqual(vehicle["assignments"][0]["status"], "excluded")

    def test_new_plan_can_reinclude_vehicles_but_old_commands_stay_fenced(self):
        # 运维确认车辆已回到旧版本后，新计划可以重新纳入
        for vid in self.installed + self.pending:
            self.service.register_vehicle(
                VehicleSnapshot(vid, "hw-a" if vid.endswith(("0", "2")) else "hw-b",
                                "sw-1.0", 90))
        self.service.register_package(SoftwarePackage(
            package_id="pkg-2", version="sw-2.1",
            compatibility=Compatibility(source_versions=frozenset({"sw-1.0"}))))
        plan2 = self.service.create_plan("pkg-2", "v1", [1.0], created_by="m")
        self.service.approve_plan(plan2, "qa")
        wave = self.service.start_next_wave(plan2)
        self.assertEqual(len(wave["commands"]), 4)
        # 新命令携带新纪元；旧计划的命令回执依旧无效
        vid = self.installed[0]
        stale = self.service.ingest_receipt(
            InstallReceipt("stale-1", self.commands[vid], vid, True))
        self.assertNotEqual(stale.disposition, ReceiptDisposition.APPLIED.value)
        # 新计划正常推进
        for i, cmd in enumerate(wave["commands"]):
            self.service.ingest_receipt(
                InstallReceipt(f"n-{i}", cmd["command_id"], cmd["vehicle_id"], True))
        self.assertEqual(self.service.get_plan(plan2)["state"],
                         PlanState.COMPLETED.value)


if __name__ == "__main__":
    unittest.main()

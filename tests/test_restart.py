"""服务重启后的继续执行：事件日志回放恢复精确状态。"""

import unittest

from helpers import ServiceTestCase, register_defaults
from rollout_control import (
    InstallReceipt,
    PlanState,
    ReceiptDisposition,
    RolloutState,
    VehicleStatus,
)


class RestartTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=6)
        self.plan_id = self.service.create_plan("pkg-1", "v1", [0.5, 1.0],
                                                created_by="m")
        self.service.approve_plan(self.plan_id, "qa")
        wave = self.service.start_next_wave(self.plan_id)
        self.wave_id = wave["wave_id"]
        self.commands = {c["vehicle_id"]: c["command_id"] for c in wave["commands"]}

    def test_resume_in_flight_wave_after_restart(self):
        vids = sorted(self.commands)
        self.service.ingest_receipt(
            InstallReceipt("ok-0", self.commands[vids[0]], vids[0], True))
        decisions_before = self.service.list_decisions(self.plan_id)

        self.reopen()
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.RUNNING.value)
        self.assertEqual(plan["waves"][0]["state"], RolloutState.RUNNING.value)
        self.assertEqual(self.service.list_decisions(self.plan_id), decisions_before)
        report = self.service.wave_report(self.plan_id, self.wave_id)
        self.assertEqual(report["metrics"]["successes"], 1)

        # 重启后继续收剩余回执，流程照常推进到完成
        for i, vid in enumerate(vids[1:], start=1):
            self.service.ingest_receipt(
                InstallReceipt(f"ok-{i}", self.commands[vid], vid, True))
        wave2 = self.service.get_plan(self.plan_id)["waves"][1]
        self.assertEqual(wave2["state"], RolloutState.RUNNING.value)
        for i, cmd in enumerate(sorted(wave2["commands"],
                                       key=lambda c: c["command_id"])):
            self.service.ingest_receipt(
                InstallReceipt(f"w2-{i}", cmd["command_id"], cmd["vehicle_id"], True))
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.COMPLETED.value)

    def test_idempotency_keys_survive_restart(self):
        vids = sorted(self.commands)
        self.service.ingest_receipt(
            InstallReceipt("ok-0", self.commands[vids[0]], vids[0], True))
        self.reopen()
        dup = self.service.ingest_receipt(
            InstallReceipt("ok-0", self.commands[vids[0]], vids[0], True))
        self.assertEqual(dup.disposition, ReceiptDisposition.APPLIED.value)
        report = self.service.wave_report(self.plan_id, self.wave_id)
        self.assertEqual(report["metrics"]["successes"], 1)

    def test_pause_state_survives_restart_and_resume_works(self):
        vids = sorted(self.commands)
        self.service.ingest_receipt(
            InstallReceipt("f-0", self.commands[vids[0]], vids[0], False, "E1"))
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.PAUSED.value)
        self.reopen()
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.PAUSED.value)
        # 修复后恢复
        retry = self.service.retry_vehicle(self.plan_id, self.wave_id, vids[0])
        self.service.ingest_receipt(
            InstallReceipt("f-1", retry.command_id, vids[0], True))
        self.service.resume_plan(self.plan_id, operator="ops-1")
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.RUNNING.value)

    def test_rollback_state_survives_restart(self):
        vids = sorted(self.commands)
        self.service.ingest_receipt(
            InstallReceipt("ok-0", self.commands[vids[0]], vids[0], True))
        self.service.rollback_plan(self.plan_id, "缺陷", operator="ops-1")
        self.reopen()
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.ROLLED_BACK.value)
        report = self.service.wave_report(self.plan_id, self.wave_id)
        status = {c["vehicle_id"]: c["status"] for c in report["cohort"]}
        self.assertEqual(status[vids[0]], VehicleStatus.ROLLING_BACK.value)
        # 回滚命令在重启后仍可受理回执
        rollback_cmd = next(c for c in plan["waves"][0]["commands"]
                            if c["kind"] == "rollback")
        rec = self.service.ingest_receipt(InstallReceipt(
            "rb-0", rollback_cmd["command_id"], rollback_cmd["vehicle_id"], True))
        self.assertEqual(rec.disposition, ReceiptDisposition.APPLIED.value)


if __name__ == "__main__":
    unittest.main()

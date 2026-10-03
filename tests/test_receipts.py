"""回执受理：幂等、重复、迟到、纪元过期与并发。"""

import unittest
from concurrent.futures import ThreadPoolExecutor

from helpers import ServiceTestCase, register_defaults
from rollout_control import (
    InstallReceipt,
    PlanState,
    ReceiptDisposition,
    RolloutState,
    VehicleStatus,
)


class ReceiptTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=10)
        self.plan_id = self.service.create_plan("pkg-1", "v1", [0.5, 1.0],
                                                created_by="m")
        self.service.approve_plan(self.plan_id, "qa")
        wave = self.service.start_next_wave(self.plan_id)
        self.wave_id = wave["wave_id"]
        self.commands = {c["vehicle_id"]: c["command_id"] for c in wave["commands"]}

    def test_same_receipt_id_is_idempotent(self):
        vid, cid = next(iter(self.commands.items()))
        first = self.service.ingest_receipt(InstallReceipt("r-1", cid, vid, True))
        again = self.service.ingest_receipt(InstallReceipt("r-1", cid, vid, True))
        self.assertIs(first, again)
        self.assertEqual(again.disposition, ReceiptDisposition.APPLIED.value)
        report = self.service.wave_report(self.plan_id, self.wave_id)
        self.assertEqual(report["metrics"]["successes"], 1)

    def test_second_receipt_for_closed_command_is_duplicate(self):
        vid, cid = next(iter(self.commands.items()))
        self.service.ingest_receipt(InstallReceipt("r-1", cid, vid, True))
        dup = self.service.ingest_receipt(InstallReceipt("r-2", cid, vid, False))
        self.assertEqual(dup.disposition, ReceiptDisposition.DUPLICATE_COMMAND.value)
        report = self.service.wave_report(self.plan_id, self.wave_id)
        # 失败回执不得覆盖已成功的结果
        self.assertEqual(report["metrics"]["successes"], 1)
        self.assertEqual(report["metrics"]["failures"], 0)

    def test_unknown_command_is_recorded_but_ignored(self):
        record = self.service.ingest_receipt(
            InstallReceipt("r-x", "cmd:nope", "V-000", True))
        self.assertEqual(record.disposition, ReceiptDisposition.UNKNOWN_COMMAND.value)

    def test_late_receipt_after_wave_completed_does_not_change_outcome(self):
        # 完成第一波（5 辆车全部成功），随后针对已终结波次补一条迟到回执。
        pending = {}
        for i, (vid, cid) in enumerate(sorted(self.commands.items())):
            if i < 4:
                self.service.ingest_receipt(InstallReceipt(f"ok-{i}", cid, vid, True))
            else:
                pending[vid] = cid
        vid, cid = next(iter(pending.items()))
        # 重试产生新命令后，旧命令的迟到回执被归类为 SUPERSEDED；
        # 这里直接让最后一辆车也成功，使波次完成，再补迟到回执。
        self.service.ingest_receipt(InstallReceipt("ok-5", cid, vid, True))
        wave = self.service.get_plan(self.plan_id)["waves"][0]
        self.assertEqual(wave["state"], RolloutState.COMPLETED.value)
        late = self.service.ingest_receipt(InstallReceipt("late-1", cid, vid, False))
        self.assertEqual(late.disposition, ReceiptDisposition.DUPLICATE_COMMAND.value)
        decisions = self.service.list_decisions(self.plan_id)
        final = [d for d in decisions if d["wave_id"] == self.wave_id][-1]
        self.assertEqual(final["metrics"]["failures"], 0)

    def test_retry_supersedes_old_command(self):
        vid, cid = next(iter(self.commands.items()))
        self.service.ingest_receipt(InstallReceipt("r-1", cid, vid, False, "E1"))
        retry = self.service.retry_vehicle(self.plan_id, self.wave_id, vid)
        self.assertNotEqual(retry.command_id, cid)
        # 旧命令的迟到回执：已取代，不再生效
        late = self.service.ingest_receipt(InstallReceipt("r-2", cid, vid, True))
        self.assertEqual(late.disposition, ReceiptDisposition.SUPERSEDED_COMMAND.value)
        # 新命令成功，车辆进入已安装
        self.service.ingest_receipt(
            InstallReceipt("r-3", retry.command_id, vid, True))
        report = self.service.wave_report(self.plan_id, self.wave_id)
        member = next(c for c in report["cohort"] if c["vehicle_id"] == vid)
        self.assertEqual(member["status"], VehicleStatus.INSTALLED.value)
        self.assertEqual(member["attempts"], 2)

    def test_concurrent_receipts_are_counted_exactly_once(self):
        # 单波次 20 辆车，8 线程并发上报：每车一条成功回执 + 前 5 车各一条重复。
        register_defaults(self.service, vehicle_count=20)
        plan_id = self.service.create_plan("pkg-1", "v1", [1.0], created_by="m")
        self.service.approve_plan(plan_id, "qa")
        wave = self.service.start_next_wave(plan_id)
        commands = [c["command_id"] for c in wave["commands"]]
        vehicles = [c["vehicle_id"] for c in wave["commands"]]

        tasks = []
        for i, (cid, vid) in enumerate(zip(commands, vehicles)):
            tasks.append(InstallReceipt(f"ok-{i}", cid, vid, True))
        for i in range(5):  # 重复回执（同幂等键与不同幂等键各覆盖）
            tasks.append(InstallReceipt(f"ok-{i}", commands[i], vehicles[i], True))
            tasks.append(InstallReceipt(f"dup-{i}", commands[i], vehicles[i], True))

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(self.service.ingest_receipt, tasks))

        # 无论并发顺序如何，被受理的命令恰好是 20 个不同的命令
        applied_commands = {r.command_id for r in records
                            if r.disposition == ReceiptDisposition.APPLIED.value}
        self.assertEqual(len(applied_commands), 20)
        plan = self.service.get_plan(plan_id)
        self.assertEqual(plan["state"], PlanState.COMPLETED.value)
        report = self.service.wave_report(plan_id, wave["wave_id"])
        self.assertEqual(report["metrics"]["successes"], 20)
        self.assertEqual(report["metrics"]["failures"], 0)

    def test_concurrent_wave_start_is_idempotent(self):
        register_defaults(self.service, vehicle_count=20)
        plan_id = self.service.create_plan("pkg-1", "v1", [1.0], created_by="m")
        self.service.approve_plan(plan_id, "qa")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.start_next_wave(plan_id),
                                    range(8)))
        command_ids = {c["command_id"] for r in results for c in r["commands"]}
        self.assertEqual(len(command_ids), 20)


if __name__ == "__main__":
    unittest.main()

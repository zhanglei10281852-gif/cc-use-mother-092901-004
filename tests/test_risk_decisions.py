"""风险判定：暂停、回滚、批次隔离、事件与健康指标。"""

import unittest

from helpers import ServiceTestCase, register_defaults
from rollout_control import (
    ConflictError,
    HealthReport,
    IncidentReport,
    InstallReceipt,
    PlanState,
    RiskPolicy,
    RolloutState,
    Severity,
    VehicleStatus,
    Verdict,
)


class PauseResumeTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=10)
        self.plan_id = self.service.create_plan("pkg-1", "v1", [0.5, 1.0],
                                                created_by="m")
        self.service.approve_plan(self.plan_id, "qa")
        wave = self.service.start_next_wave(self.plan_id)
        self.wave_id = wave["wave_id"]
        self.commands = {c["vehicle_id"]: c["command_id"] for c in wave["commands"]}

    def test_failure_rate_triggers_pause_then_resume_after_retry(self):
        vids = sorted(self.commands)
        # 5 辆车中 1 辆失败：风险率 0.2 达到暂停阈值
        self.service.ingest_receipt(
            InstallReceipt("f-1", self.commands[vids[0]], vids[0], False, "E_FLASH"))
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.PAUSED.value)
        self.assertEqual(plan["waves"][0]["state"], RolloutState.PAUSED.value)

        # 恢复条件：手动确认未完成；严重事件已满足
        report = self.service.wave_report(self.plan_id, self.wave_id)
        conditions = {c["id"]: c for c in report["recovery_conditions"]}
        self.assertTrue(conditions["critical_incidents_clear"]["met"])
        self.assertFalse(conditions["manual_resume"]["met"])
        self.assertFalse(conditions["risk_rate_below_pause_threshold"]["met"])

        # 暂停期间重试失败车辆并修复，风险率归零
        retry = self.service.retry_vehicle(self.plan_id, self.wave_id, vids[0])
        self.service.ingest_receipt(
            InstallReceipt("f-2", retry.command_id, vids[0], True))
        self.service.resume_plan(self.plan_id, operator="ops-1")
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.RUNNING.value)

        # 其余车辆成功 -> 第一波完成并自动进入第二波
        for i, vid in enumerate(vids[1:]):
            self.service.ingest_receipt(
                InstallReceipt(f"ok-{i}", self.commands[vid], vid, True))
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["waves"][0]["state"], RolloutState.COMPLETED.value)
        self.assertEqual(plan["waves"][1]["state"], RolloutState.RUNNING.value)

    def test_resume_rejected_while_blocking_condition_unmet(self):
        vids = sorted(self.commands)
        self.service.file_incident(IncidentReport(
            "inc-1", Severity.CRITICAL, "用户报告转向异常", vehicle_id=vids[0]))
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.PAUSED.value)
        with self.assertRaises(ConflictError):
            self.service.resume_plan(self.plan_id, operator="ops-1")
        self.service.resolve_incident("inc-1")
        self.service.resume_plan(self.plan_id, operator="ops-1")
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.RUNNING.value)

    def test_unhealthy_vehicle_counts_toward_risk(self):
        vids = sorted(self.commands)
        self.service.ingest_health(HealthReport(vids[0], 40, reported_at=1.0))
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.PAUSED.value)
        decisions = self.service.list_decisions(self.plan_id)
        self.assertEqual(decisions[-1]["verdict"], Verdict.PAUSE.value)
        self.assertEqual(decisions[-1]["metrics"]["unhealthy"], 1)
        # 健康恢复后允许恢复
        self.service.ingest_health(HealthReport(vids[0], 95, reported_at=2.0))
        self.service.resume_plan(self.plan_id, operator="ops-1")
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.RUNNING.value)


class RollbackTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=8)
        # 提高批次隔离门槛，避免本用例触发隔离而干扰回滚路径
        self.service.register_policy(RiskPolicy(
            version="v-rb",
            pause_failure_rate=0.2,
            rollback_failure_rate=0.5,
            batch_quarantine_rate=1.0,
            min_batch_sample=3,
        ))
        self.plan_id = self.service.create_plan("pkg-1", "v-rb", [0.5, 1.0],
                                                created_by="m")
        self.service.approve_plan(self.plan_id, "qa")
        wave = self.service.start_next_wave(self.plan_id)
        self.wave_id = wave["wave_id"]
        self.commands = {c["vehicle_id"]: c["command_id"] for c in wave["commands"]}

    def test_high_failure_rate_triggers_rollback(self):
        vids = sorted(self.commands)
        # 2 成功 + 2 失败：风险率 0.5 达到回滚阈值
        self.service.ingest_receipt(
            InstallReceipt("ok-0", self.commands[vids[0]], vids[0], True))
        self.service.ingest_receipt(
            InstallReceipt("ok-1", self.commands[vids[1]], vids[1], True))
        self.service.ingest_receipt(
            InstallReceipt("f-0", self.commands[vids[2]], vids[2], False, "E1"))
        self.service.ingest_receipt(
            InstallReceipt("f-1", self.commands[vids[3]], vids[3], False, "E2"))
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["state"], PlanState.ROLLED_BACK.value)
        self.assertEqual(plan["waves"][0]["state"], RolloutState.ROLLED_BACK.value)
        self.assertEqual(plan["waves"][1]["state"], RolloutState.ROLLED_BACK.value)

        # 已安装车辆收到回滚命令（新纪元），未安装车辆被排除
        report = self.service.wave_report(self.plan_id, self.wave_id)
        status = {c["vehicle_id"]: c["status"] for c in report["cohort"]}
        self.assertEqual(status[vids[0]], VehicleStatus.ROLLING_BACK.value)
        self.assertEqual(status[vids[1]], VehicleStatus.ROLLING_BACK.value)
        self.assertEqual(status[vids[2]], VehicleStatus.EXCLUDED.value)
        self.assertEqual(status[vids[3]], VehicleStatus.EXCLUDED.value)

        # 回滚回执同样幂等
        rollback_cmds = [c for c in plan["waves"][0]["commands"]
                         if c["kind"] == "rollback"]
        self.assertEqual(len(rollback_cmds), 2)
        for i, cmd in enumerate(rollback_cmds):
            rec = self.service.ingest_receipt(InstallReceipt(
                f"rb-{i}", cmd["command_id"], cmd["vehicle_id"], True))
            self.assertEqual(rec.disposition, "applied")
            dup = self.service.ingest_receipt(InstallReceipt(
                f"rb-{i}", cmd["command_id"], cmd["vehicle_id"], True))
            self.assertIs(dup, rec)  # 同一幂等键返回首次受理结果
            self.assertEqual(dup.duplicate_count, 1)
        report = self.service.wave_report(self.plan_id, self.wave_id)
        status = {c["vehicle_id"]: c["status"] for c in report["cohort"]}
        self.assertEqual(status[vids[0]], VehicleStatus.ROLLED_BACK.value)

    def test_critical_incidents_trigger_rollback(self):
        vids = sorted(self.commands)
        self.service.file_incident(IncidentReport(
            "inc-1", Severity.CRITICAL, "制动异常", vehicle_id=vids[0]))
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.PAUSED.value)
        self.service.file_incident(IncidentReport(
            "inc-2", Severity.CRITICAL, "转向失效", vehicle_id=vids[1]))
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.ROLLED_BACK.value)

    def test_manual_rollback(self):
        self.service.rollback_plan(self.plan_id, "监管要求", operator="ops-9")
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.ROLLED_BACK.value)


class QuarantineTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=6)
        self.plan_id = self.service.create_plan("pkg-1", "v1", [1.0], created_by="m")
        self.service.approve_plan(self.plan_id, "qa")
        wave = self.service.start_next_wave(self.plan_id)
        self.wave_id = wave["wave_id"]
        self.commands = {c["vehicle_id"]: c["command_id"] for c in wave["commands"]}

    def test_anomalous_batch_is_quarantined_and_rollout_continues(self):
        # hw-b 批次两辆车均失败（样本 2，失败率 100% >= 50%）
        hw_b = sorted(v for v in self.commands if v in ("V-001", "V-003", "V-005"))
        self.service.ingest_receipt(
            InstallReceipt("f-1", self.commands[hw_b[0]], hw_b[0], False, "E1"))
        self.service.ingest_receipt(
            InstallReceipt("f-2", self.commands[hw_b[1]], hw_b[1], False, "E2"))
        report = self.service.wave_report(self.plan_id, self.wave_id)
        status = {c["vehicle_id"]: c["status"] for c in report["cohort"]}
        # 整个 hw-b 批次（含尚未回报的 V-005）被隔离
        for vid in hw_b:
            self.assertEqual(status[vid], VehicleStatus.QUARANTINED.value)
        # hw-a 车辆不受影响，波次继续
        self.assertEqual(report["state"], RolloutState.RUNNING.value)
        self.assertIn("hw-b", report["quarantined_batches"])
        verdicts = [d["verdict"] for d in report["decisions"]]
        self.assertIn(Verdict.QUARANTINE_BATCH.value, verdicts)

        # 剩余 hw-a 车辆成功后波次完成
        for i, vid in enumerate(sorted(v for v in self.commands
                                       if v not in hw_b)):
            self.service.ingest_receipt(
                InstallReceipt(f"ok-{i}", self.commands[vid], vid, True))
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.COMPLETED.value)

    def test_quarantined_batch_is_excluded_from_new_plans_until_cleared(self):
        hw_b = ("V-001", "V-003")
        for i, vid in enumerate(hw_b):
            self.service.ingest_receipt(
                InstallReceipt(f"f-{i}", self.commands[vid], vid, False, "E1"))
        # 新计划自动排除隔离批次
        plan2 = self.service.create_plan("pkg-1", "v1", [1.0], created_by="m")
        plan = self.service.get_plan(plan2)
        self.assertEqual(plan["waves"][0]["cohort_size"], 3)
        excluded = {e["vehicle_id"] for e in plan["exclusions"]}
        self.assertTrue({"V-001", "V-003", "V-005"} <= excluded)
        # 解除隔离后新计划重新纳入
        self.service.clear_quarantine("hw-b", operator="ops-1")
        plan3 = self.service.create_plan("pkg-1", "v1", [1.0], created_by="m")
        self.assertEqual(self.service.get_plan(plan3)["waves"][0]["cohort_size"], 6)


if __name__ == "__main__":
    unittest.main()

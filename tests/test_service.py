import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from rollout_control import (
    Compatibility,
    ConflictError,
    NotFoundError,
    RiskPolicy,
    RolloutControlService,
    RolloutState,
    SoftwarePackage,
    VehicleSnapshot,
)


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)

    def iso(self):
        return self.t.isoformat()


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "rollout.db")
        self.clock = FakeClock()
        self.svc = RolloutControlService(self.db, now=self.clock)
        self.svc.register_policy(
            RiskPolicy(
                policy_id="p",
                version=1,
                max_failure_rate=0.2,
                max_critical_incidents=0,
                max_incidents=10,
                min_avg_health_score=0.0,
                receipt_timeout_seconds=3600,
            )
        )

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 辅助 ----------
    def make_package(self, package_id="pkg-1", model="sedan-x", **compat_kw):
        compat = Compatibility(**compat_kw)
        self.svc.register_package(
            SoftwarePackage(package_id=package_id, model=model, version="2.0.0", compatibility=compat)
        )
        return package_id

    def add_vehicle(self, vid, model="sedan-x", hw="hw-a", sw="sw-1", battery=80,
                    online=True, health=0.9, reported_at=None):
        self.svc.register_snapshot(
            VehicleSnapshot(
                vehicle_id=vid,
                hardware_batch=hw,
                software_version=sw,
                battery_percent=battery,
                model=model,
                online=online,
                health_score=health,
                reported_at=reported_at or self.clock.iso(),
            )
        )

    def add_fleet(self, n, prefix="V", **kw):
        for i in range(1, n + 1):
            self.add_vehicle(f"{prefix}-{i}", **kw)

    def make_wave(self, wave_id="W1", package_id="pkg-1", seq=1, target=1.0, policy_id="p"):
        self.svc.create_wave(wave_id, package_id, seq, target, policy_id=policy_id)
        self.svc.approve_wave(wave_id)
        return self.svc.start_wave(wave_id)

    def command_ids(self, wave_id, kind="install"):
        return [
            c["command_id"]
            for c in self.svc.list_commands(wave_id)
            if c["kind"] == kind
        ]

    def succeed_all(self, wave_id, tag="ok"):
        for i, cid in enumerate(self.command_ids(wave_id)):
            self.svc.submit_receipt(f"r-{wave_id}-{tag}-{i}", cid, "success")


class RegistrationTests(ServiceTestBase):
    def test_default_policy_is_preregistered(self):
        policies = [p for p in self.svc.list_policies() if p["policy_id"] == "default"]
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0]["version"], 1)

    def test_package_reregistration_is_idempotent_but_not_overwritable(self):
        self.make_package()
        result = self.svc.register_package(  # 同内容重复登记：幂等
            SoftwarePackage(package_id="pkg-1", model="sedan-x", version="2.0.0")
        )
        self.assertFalse(result["registered"])
        with self.assertRaises(ConflictError):
            self.svc.register_package(
                SoftwarePackage(package_id="pkg-1", model="sedan-x", version="9.9.9")
            )

    def test_policy_version_is_immutable(self):
        with self.assertRaises(ConflictError):
            self.svc.register_policy(
                RiskPolicy(policy_id="p", version=1, max_failure_rate=0.99)
            )
        # 换版必须递增版本号
        self.svc.register_policy(RiskPolicy(policy_id="p", version=2, max_failure_rate=0.3))
        wave_policies = {p["version"] for p in self.svc.list_policies() if p["policy_id"] == "p"}
        self.assertEqual(wave_policies, {1, 2})

    def test_stale_snapshot_update_is_ignored(self):
        self.add_vehicle("V-1", battery=80)
        older = (self.clock.t - timedelta(hours=2)).isoformat()
        result = self.svc.register_snapshot(
            VehicleSnapshot("V-1", "hw-a", "sw-1", 10, model="sedan-x", reported_at=older)
        )
        self.assertFalse(result["updated"])
        self.assertEqual(self.svc.get_vehicle("V-1")["snapshot"]["battery_percent"], 80)


class EligibilityTests(ServiceTestBase):
    def test_selection_respects_compat_freshness_and_flags(self):
        stale = (self.clock.t - timedelta(hours=2)).isoformat()
        self.make_package(
            allowed_hardware_batches=("hw-a",),
            allowed_from_versions=("sw-1",),
            min_battery_percent=50,
            require_online=True,
            min_health_score=0.5,
            max_snapshot_age_seconds=3600,
        )
        self.add_vehicle("V-ok")
        self.add_vehicle("V-battery", battery=30)
        self.add_vehicle("V-offline", online=False)
        self.add_vehicle("V-hw", hw="hw-b")
        self.add_vehicle("V-sw", sw="sw-0")
        self.add_vehicle("V-health", health=0.2)
        self.add_vehicle("V-stale", reported_at=stale)
        self.add_vehicle("V-quar")
        self.svc.quarantine_vehicle("V-quar", "人工抽检隔离")

        self.make_wave("W1")
        members = {m.vehicle_id: m for m in self.svc.get_members("W1")}

        self.assertTrue(members["V-ok"].included)
        self.assertFalse(members["V-battery"].included)
        self.assertTrue(any("电量" in r for r in members["V-battery"].reasons))
        self.assertTrue(any("离线" in r for r in members["V-offline"].reasons))
        self.assertTrue(any("硬件批次" in r for r in members["V-hw"].reasons))
        self.assertTrue(any("可升级版本" in r for r in members["V-sw"].reasons))
        self.assertTrue(any("健康分" in r for r in members["V-health"].reasons))
        self.assertTrue(any("快照过旧" in r for r in members["V-stale"].reasons))
        self.assertTrue(any("隔离" in r for r in members["V-quar"].reasons))
        # 入组车辆的理由同样可追溯
        self.assertTrue(any("入选本波次" in r for r in members["V-ok"].reasons))

    def test_wave_requires_approval_and_ordered_progression(self):
        self.make_package()
        self.add_fleet(4)
        self.svc.create_wave("W1", "pkg-1", 1, 0.5, policy_id="p")
        self.svc.create_wave("W2", "pkg-1", 2, 1.0, policy_id="p")
        with self.assertRaises(ConflictError):
            self.svc.start_wave("W1")  # 未审批
        self.svc.approve_wave("W1")
        self.svc.approve_wave("W2")
        with self.assertRaises(ConflictError):
            self.svc.start_wave("W2")  # 前序波次未完结
        self.svc.start_wave("W1")
        self.assertEqual(self.svc.get_wave("W1").members_included, 2)
        self.succeed_all("W1")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)
        self.svc.start_wave("W2")
        # 第二波补齐到 100%：只新增其余两辆
        self.assertEqual(self.svc.get_wave("W2").members_included, 2)
        included = {m.vehicle_id for m in self.svc.get_members("W2") if m.included}
        self.assertEqual(included, {"V-3", "V-4"})


class CommandAndReceiptTests(ServiceTestBase):
    def test_install_commands_are_deterministic_and_idempotent(self):
        self.make_package()
        self.add_fleet(3)
        self.make_wave("W1")
        commands = self.svc.list_commands("W1")
        self.assertEqual(len(commands), 3)
        self.assertEqual(
            {c["command_id"] for c in commands},
            {f"cmd-W1-V-{i}-install" for i in (1, 2, 3)},
        )
        # 波次已运行，不能重复启动重复下发
        with self.assertRaises(ConflictError):
            self.svc.start_wave("W1")
        self.assertEqual(len(self.svc.list_commands("W1")), 3)

    def test_duplicate_receipt_is_counted_once(self):
        self.make_package()
        self.add_fleet(2)
        self.make_wave("W1")
        cid = self.command_ids("W1")[0]
        first = self.svc.submit_receipt("r-1", cid, "success")
        self.assertFalse(first["duplicate"])
        again = self.svc.submit_receipt("r-1", cid, "success")
        self.assertTrue(again["duplicate"])
        receipts = [r for r in self.svc.get_receipts("W1") if not r["duplicate"]]
        self.assertEqual(len(receipts), 1)

    def test_same_command_new_receipt_id_does_not_double_count(self):
        self.make_package()
        self.add_fleet(2)
        self.make_wave("W1")
        cid = self.command_ids("W1")[0]
        self.svc.submit_receipt("r-1", cid, "success")
        second = self.svc.submit_receipt("r-2", cid, "success")
        self.assertTrue(second["duplicate"])  # 命令已完结，回执不重复计数
        budget = self.svc.get_risk_budget("W1")
        self.assertEqual(budget.succeeded, 1)

    def test_conflicting_duplicate_keeps_first_and_logs_anomaly(self):
        self.make_package()
        self.add_fleet(2)
        self.make_wave("W1")
        cid = self.command_ids("W1")[0]
        self.svc.submit_receipt("r-1", cid, "success")
        conflict = self.svc.submit_receipt("r-1", cid, "failed")
        self.assertTrue(conflict["conflict"])
        anomalies = self.svc.list_anomalies()
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["kind"], "receipt_conflict")
        # 先到的成功回执保留
        budget = self.svc.get_risk_budget("W1")
        self.assertEqual(budget.succeeded, 1)
        self.assertEqual(budget.failed, 0)

    def test_receipt_for_unknown_command_is_rejected(self):
        with self.assertRaises(NotFoundError):
            self.svc.submit_receipt("r-x", "cmd-nope", "success")

    def test_late_receipt_is_flagged_after_timeout(self):
        self.make_package()
        self.add_fleet(2)
        self.make_wave("W1")
        cid = self.command_ids("W1")[0]
        self.clock.advance(hours=2)  # 超过 3600 秒回执超时
        result = self.svc.submit_receipt("r-late", cid, "success")
        self.assertTrue(result["late"])
        self.assertEqual(self.svc.get_risk_budget("W1").late_receipts, 1)

    def test_late_receipt_after_completion_does_not_change_decisions(self):
        self.make_package()
        self.add_fleet(2)
        self.make_wave("W1")
        self.succeed_all("W1")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)
        decisions_before = self.svc.get_decisions("W1")
        # 迟到的重复回执（新 receipt_id）到达已完结波次
        cid = self.command_ids("W1")[0]
        result = self.svc.submit_receipt("r-late-extra", cid, "success")
        self.assertTrue(result["late"])
        self.assertEqual(self.svc.get_decisions("W1"), decisions_before)
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)


class RiskDecisionTests(ServiceTestBase):
    def test_partial_failure_within_budget_completes(self):
        self.make_package()
        self.add_fleet(10)
        self.make_wave("W1")  # 预算 floor(10 * 0.2) = 2
        cids = self.command_ids("W1")
        self.svc.submit_receipt("r-f1", cids[0], "failed", "刷写中断")
        for i, cid in enumerate(cids[1:]):
            self.svc.submit_receipt(f"r-ok-{i}", cid, "success")
        wave = self.svc.get_wave("W1")
        self.assertEqual(wave.state, RolloutState.COMPLETED)
        budget = self.svc.get_risk_budget("W1")
        self.assertEqual(budget.failed, 1)
        self.assertTrue(budget.within_budget)

    def test_failure_breach_auto_pauses_and_blocks_plain_resume(self):
        self.make_package()
        self.add_fleet(10)
        self.make_wave("W1")  # 预算 floor(10 * 0.2) = 2
        cids = self.command_ids("W1")
        self.svc.submit_receipt("r-f1", cids[0], "failed")
        self.svc.submit_receipt("r-f2", cids[1], "failed")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.RUNNING)
        self.svc.submit_receipt("r-f3", cids[2], "failed")  # 超过预算 2
        wave = self.svc.get_wave("W1")
        self.assertEqual(wave.state, RolloutState.PAUSED)
        decisions = self.svc.get_decisions("W1")
        self.assertEqual(decisions[-1].action, "pause")
        self.assertEqual(decisions[-1].policy_version, 1)
        # 恢复条件未满足，普通恢复被拒绝
        with self.assertRaises(ConflictError):
            self.svc.resume_wave("W1")
        conditions = {c.name: c for c in self.svc.get_recovery_conditions("W1")}
        self.assertFalse(conditions["失败预算"].met)
        self.assertEqual(conditions["失败预算"].current, "3")

    def test_resume_with_override_is_audited_and_wave_completes(self):
        self.make_package()
        self.add_fleet(10)
        self.make_wave("W1")
        cids = self.command_ids("W1")
        for i in range(3):
            self.svc.submit_receipt(f"r-f{i}", cids[i], "failed")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.PAUSED)
        self.svc.resume_wave("W1", actor="ops-li", override_reason="确认为偶发刷写中断，人工兜底继续")
        wave = self.svc.get_wave("W1")
        self.assertEqual(wave.state, RolloutState.RUNNING)
        self.assertTrue(wave.manual_override)
        transitions = self.svc.get_transitions("W1")
        self.assertIn("人工强制恢复", transitions[-1].reason)
        # 兜底期间超支仅记录，不再自动暂停
        for i, cid in enumerate(cids[3:]):
            self.svc.submit_receipt(f"r-ok-{i}", cid, "success")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)

    def test_critical_incident_auto_pauses(self):
        self.make_package()
        self.add_fleet(4)
        self.make_wave("W1")
        self.svc.report_incident("inc-1", "V-1", "critical", "车主报障：辅助驾驶异常退出")
        wave = self.svc.get_wave("W1")
        self.assertEqual(wave.state, RolloutState.PAUSED)
        self.assertIn("严重事件", self.svc.get_decisions("W1")[-1].reason)
        # 重复事件报告幂等
        dup = self.svc.report_incident("inc-1", "V-1", "critical")
        self.assertTrue(dup["duplicate"])

    def test_incident_on_non_member_does_not_pause(self):
        self.make_package()
        self.add_fleet(4)
        self.make_wave("W1", target=0.5)  # 只入组 V-1、V-2
        self.svc.report_incident("inc-9", "V-4", "critical", "未入组车辆报障")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.RUNNING)

    def test_health_drop_auto_pauses(self):
        self.svc.register_policy(
            RiskPolicy(policy_id="strict-health", version=1, max_failure_rate=0.5,
                       min_avg_health_score=0.5)
        )
        self.make_package()
        self.add_fleet(2)
        self.make_wave("W1", policy_id="strict-health")
        self.svc.report_health("V-1", 0.2)
        self.svc.report_health("V-2", 0.1)
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.PAUSED)
        self.assertIn("健康分", self.svc.get_decisions("W1")[-1].reason)


class RollbackAndQuarantineTests(ServiceTestBase):
    def test_rollback_blocks_readvance_and_issues_rollback_commands(self):
        self.make_package()
        self.add_fleet(4)
        self.make_wave("W1", target=0.5)
        self.succeed_all("W1")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)

        self.svc.create_wave("W2", "pkg-1", 2, 1.0, policy_id="p")
        self.svc.approve_wave("W2")
        self.svc.start_wave("W2")
        cids = self.command_ids("W2")
        self.svc.submit_receipt("r-w2-0", cids[0], "success")  # V-3 已装
        self.svc.rollback_wave("W2", reason="V-3 升级后动力域报错")

        wave2 = self.svc.get_wave("W2")
        self.assertEqual(wave2.state, RolloutState.ROLLED_BACK)
        # 已装车辆收到幂等回滚命令，未装车辆命令被取消
        rollback_cmds = self.command_ids("W2", kind="rollback")
        self.assertEqual(rollback_cmds, [f"cmd-W2-V-3-rollback"])
        install = {c["vehicle_id"]: c["status"] for c in self.svc.list_commands("W2") if c["kind"] == "install"}
        self.assertEqual(install["V-4"], "cancelled")
        # 回滚车辆被封锁
        for vid in ("V-3", "V-4"):
            flags = self.svc.get_vehicle(vid)["flags"]
            self.assertIn("pkg-1", flags["blocked_packages"])
        # 旧波次不可重新推进
        with self.assertRaises(ConflictError):
            self.svc.start_wave("W2")
        # 迟到回执到达已取消命令：不改变回滚决策
        late = self.svc.submit_receipt("r-w2-late", cids[1], "success")
        self.assertTrue(late["late"])
        self.assertEqual(self.svc.get_wave("W2").state, RolloutState.ROLLED_BACK)

        # 同包新波次可以启动（前序已回滚），但被封锁车辆不得再入组
        self.svc.create_wave("W3", "pkg-1", 3, 1.0, policy_id="p")
        self.svc.approve_wave("W3")
        self.svc.start_wave("W3")
        members = {m.vehicle_id: m for m in self.svc.get_members("W3")}
        self.assertFalse(members["V-3"].included)
        self.assertTrue(any("封锁" in r for r in members["V-3"].reasons))
        self.assertFalse(members["V-4"].included)
        # V-1、V-2 已在 W1 推送过，W3 无人可升，直接完结
        self.assertEqual(self.svc.get_wave("W3").state, RolloutState.COMPLETED)

    def test_quarantine_wave_isolates_member_vehicles(self):
        self.make_package()
        self.add_fleet(3)
        self.make_wave("W1")
        self.svc.quarantine_wave("W1", reason="批次 hw-a 疑似共性故障")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.QUARANTINED)
        for vid in ("V-1", "V-2", "V-3"):
            flags = self.svc.get_vehicle(vid)["flags"]
            self.assertTrue(flags["quarantined"])
        # 被隔离车辆在新软件包的波次中同样被排除
        self.make_package(package_id="pkg-2")
        self.svc.create_wave("W9", "pkg-2", 1, 1.0, policy_id="p")
        self.svc.approve_wave("W9")
        self.svc.start_wave("W9")
        members = self.svc.get_members("W9")
        self.assertFalse(any(m.included for m in members))
        self.assertTrue(any("隔离" in r for m in members for r in m.reasons))


class PolicyVersionTests(ServiceTestBase):
    def test_rule_change_does_not_alter_completed_decisions(self):
        self.make_package()
        self.add_fleet(4)
        # v1 宽松：预算 floor(2 * 0.5) = 1
        self.svc.register_policy(RiskPolicy(policy_id="p2", version=1, max_failure_rate=0.5))
        self.svc.create_wave("W1", "pkg-1", 1, 0.5, policy_id="p2")
        self.svc.approve_wave("W1")
        self.svc.start_wave("W1")
        cids = self.command_ids("W1")
        self.svc.submit_receipt("r-f", cids[0], "failed")
        self.svc.submit_receipt("r-ok", cids[1], "success")
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)
        completed_decision = self.svc.get_decisions("W1")[-1]
        self.assertEqual(completed_decision.action, "complete")
        self.assertEqual(completed_decision.policy_version, 1)

        # 规则换版收紧：同内容重登 v1 被拒，v2 生效
        with self.assertRaises(ConflictError):
            self.svc.register_policy(RiskPolicy(policy_id="p2", version=1, max_failure_rate=0.01))
        self.svc.register_policy(RiskPolicy(policy_id="p2", version=2, max_failure_rate=0.01))

        # 已完成的决策不被改写
        self.assertEqual(self.svc.get_decisions("W1")[-1], completed_decision)
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)

        # 新波次固化 v2：同样的失败模式立即触发暂停
        self.svc.create_wave("W2", "pkg-1", 2, 1.0, policy_id="p2")
        self.assertEqual(self.svc.get_wave("W2").policy_version, 2)
        self.svc.approve_wave("W2")
        self.svc.start_wave("W2")
        cids2 = self.command_ids("W2")
        self.svc.submit_receipt("r2-f", cids2[0], "failed")
        self.assertEqual(self.svc.get_wave("W2").state, RolloutState.PAUSED)
        self.assertEqual(self.svc.get_decisions("W2")[-1].policy_version, 2)


class RestartAndConcurrencyTests(ServiceTestBase):
    def test_service_restart_resumes_inflight_wave(self):
        self.make_package()
        self.add_fleet(4)
        self.make_wave("W1")
        cids = self.command_ids("W1")
        self.svc.submit_receipt("r-1", cids[0], "success")
        self.svc.submit_receipt("r-2", cids[1], "success")

        # 模拟服务重启：同一数据库文件上重建实例
        restarted = RolloutControlService(self.db, now=self.clock)
        wave = restarted.get_wave("W1")
        self.assertEqual(wave.state, RolloutState.RUNNING)
        self.assertEqual(wave.receipts_received, 2)
        # 继续接收回执直至完结
        restarted.submit_receipt("r-3", cids[2], "success")
        restarted.submit_receipt("r-4", cids[3], "success")
        self.assertEqual(restarted.get_wave("W1").state, RolloutState.COMPLETED)
        # 完整状态迁移历史在重启后仍可追溯
        states = [t.to_state for t in restarted.get_transitions("W1")]
        self.assertEqual(
            states,
            [
                RolloutState.AWAITING_APPROVAL,
                RolloutState.APPROVED,
                RolloutState.RUNNING,
                RolloutState.COMPLETED,
            ],
        )
        # 重启后续跑同样保持回执幂等
        dup = restarted.submit_receipt("r-4", cids[3], "success")
        self.assertTrue(dup["duplicate"])

    def test_concurrent_duplicate_receipts_are_exactly_once(self):
        self.make_package()
        self.add_fleet(6)
        self.make_wave("W1")
        cids = self.command_ids("W1")
        errors = []

        def worker(n):
            try:
                # 每条命令并发上报：同 receipt_id 两次（去重不落地）+ 两个新 receipt_id（落地但标记重复）
                self.svc.submit_receipt(f"r-shared-{n}", cids[n], "success")
                self.svc.submit_receipt(f"r-shared-{n}", cids[n], "success")
                self.svc.submit_receipt(f"r-extra-a-{n}", cids[n], "success")
                self.svc.submit_receipt(f"r-extra-b-{n}", cids[n], "success")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        receipts = self.svc.get_receipts("W1")
        effective = [r for r in receipts if not r["duplicate"]]
        duplicates = [r for r in receipts if r["duplicate"]]
        self.assertEqual(len(effective), 6)
        self.assertEqual(len(duplicates), 12)
        budget = self.svc.get_risk_budget("W1")
        self.assertEqual(budget.succeeded, 6)
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)

    def test_concurrent_mixed_receipts_keep_counts_consistent(self):
        self.make_package()
        self.add_fleet(20)
        self.make_wave("W1")
        cids = self.command_ids("W1")
        errors = []

        def worker(n):
            try:
                status = "failed" if n % 10 == 0 else "success"
                self.svc.submit_receipt(f"r-{n}", cids[n], status)
            except Exception as exc:  # 自动暂停后迟到回执属正常流程，不应报错
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        budget = self.svc.get_risk_budget("W1")
        self.assertEqual(budget.succeeded + budget.failed, 20)
        self.assertEqual(budget.failed, 2)
        # 预算 floor(20*0.2)=4，未超支，波次应完结
        self.assertEqual(self.svc.get_wave("W1").state, RolloutState.COMPLETED)


if __name__ == "__main__":
    unittest.main()

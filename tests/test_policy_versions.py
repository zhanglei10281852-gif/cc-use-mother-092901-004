"""规则换版：已完成的判定不可被静默改变，迁移只影响后续判定。"""

import unittest

from helpers import ServiceTestCase, register_defaults
from rollout_control import (
    ConflictError,
    InstallReceipt,
    NotFoundError,
    PlanState,
    RiskPolicy,
    RolloutState,
    Verdict,
)


class PolicyVersionTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        register_defaults(self.service, vehicle_count=4)
        # v1 宽松：50% 风险率才暂停
        self.service.register_policy(RiskPolicy(
            version="v-loose",
            pause_failure_rate=0.6,
            rollback_failure_rate=0.9,
            batch_quarantine_rate=0.9,
            min_batch_sample=2,
        ))
        # v2 严格：20% 即暂停
        self.service.register_policy(RiskPolicy(
            version="v-strict",
            pause_failure_rate=0.2,
            rollback_failure_rate=0.9,
            batch_quarantine_rate=0.9,
            min_batch_sample=2,
        ))
        self.plan_id = self.service.create_plan("pkg-1", "v-loose", [0.5, 1.0],
                                                created_by="m")
        self.service.approve_plan(self.plan_id, "qa")

    def _fail_first_vehicle(self, wave_commands):
        cmd = sorted(wave_commands, key=lambda c: c["command_id"])[0]
        self.service.ingest_receipt(
            InstallReceipt(f"f-{cmd['command_id']}", cmd["command_id"],
                           cmd["vehicle_id"], False, "E1"))

    def test_completed_wave_keeps_original_verdict_after_policy_change(self):
        # 第一波 2 辆车：1 失败 -> v-loose 下风险率 0.5 < 0.6，判定继续
        wave1 = self.service.start_next_wave(self.plan_id)
        self._fail_first_vehicle(wave1["commands"])
        for i, cmd in enumerate(sorted(wave1["commands"],
                                       key=lambda c: c["command_id"])[1:]):
            self.service.ingest_receipt(
                InstallReceipt(f"ok-{i}", cmd["command_id"], cmd["vehicle_id"], True))
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["waves"][0]["state"], RolloutState.COMPLETED.value)

        wave1_decisions = [d for d in self.service.list_decisions(self.plan_id)
                           if d["wave_id"] == f"{self.plan_id}-w1"]
        final = wave1_decisions[-1]
        self.assertEqual(final["verdict"], Verdict.CONTINUE.value)
        self.assertEqual(final["policy_version"], "v-loose")

        # 迁移到严格规则：已完成的波次与判定保持原样
        self.service.migrate_plan_policy(self.plan_id, "v-strict",
                                         "安全团队收紧阈值")
        plan = self.service.get_plan(self.plan_id)
        self.assertEqual(plan["policy_version"], "v-strict")
        self.assertEqual(plan["waves"][0]["state"], RolloutState.COMPLETED.value)
        wave1_after = [d for d in self.service.list_decisions(self.plan_id)
                       if d["wave_id"] == f"{self.plan_id}-w1"]
        self.assertEqual(wave1_after, wave1_decisions)

        # 第二波按新规则判定：同样的 1/2 失败率现在触发暂停
        plan_wave2 = plan["waves"][1]
        self.assertEqual(plan_wave2["state"], RolloutState.RUNNING.value)
        self._fail_first_vehicle(plan_wave2["commands"])
        self.assertEqual(self.service.get_plan(self.plan_id)["state"],
                         PlanState.PAUSED.value)
        wave2_decisions = [d for d in self.service.list_decisions(self.plan_id)
                           if d["wave_id"] == f"{self.plan_id}-w2"]
        self.assertEqual(wave2_decisions[-1]["verdict"], Verdict.PAUSE.value)
        self.assertEqual(wave2_decisions[-1]["policy_version"], "v-strict")

    def test_policy_versions_are_immutable(self):
        with self.assertRaises(ConflictError):
            self.service.register_policy(RiskPolicy(
                version="v-loose",
                pause_failure_rate=0.1,  # 同版本号不同内容
                rollback_failure_rate=0.9,
                batch_quarantine_rate=0.9,
            ))

    def test_migration_requires_existing_version_and_open_plan(self):
        with self.assertRaises(NotFoundError):
            self.service.migrate_plan_policy(self.plan_id, "v-missing", "x")
        with self.assertRaises(ConflictError):
            self.service.migrate_plan_policy(self.plan_id, "v-loose", "x")

    def test_completed_decisions_survive_restart(self):
        wave1 = self.service.start_next_wave(self.plan_id)
        self._fail_first_vehicle(wave1["commands"])
        for i, cmd in enumerate(sorted(wave1["commands"],
                                       key=lambda c: c["command_id"])[1:]):
            self.service.ingest_receipt(
                InstallReceipt(f"ok-{i}", cmd["command_id"], cmd["vehicle_id"], True))
        before = self.service.list_decisions(self.plan_id)
        self.service.migrate_plan_policy(self.plan_id, "v-strict", "收紧")

        self.reopen()
        after = self.service.list_decisions(self.plan_id)
        self.assertEqual(before, after)
        wave1_decisions = [d for d in after if d["wave_id"] == f"{self.plan_id}-w1"]
        self.assertEqual(wave1_decisions[-1]["policy_version"], "v-loose")
        self.assertEqual(wave1_decisions[-1]["verdict"], Verdict.CONTINUE.value)


if __name__ == "__main__":
    unittest.main()

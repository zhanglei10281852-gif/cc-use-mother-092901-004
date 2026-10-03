import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from rollout_control import RolloutControlService, make_server


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = str(Path(cls.tmp.name) / "api.db")
        cls.service = RolloutControlService(db)
        cls.server = make_server(cls.service, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class ApiFlowTests(ApiTestBase):
    def test_full_rollout_flow_over_http(self):
        # 登记策略 / 软件包 / 车辆快照
        status, _ = self.call("POST", "/api/policies", {
            "policy_id": "api-p", "version": 1, "max_failure_rate": 0.5,
        })
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/api/packages", {
            "package_id": "api-pkg", "model": "suv-y", "version": "3.1.0",
            "compatibility": {"min_battery_percent": 40, "require_online": True},
        })
        self.assertEqual(status, 200)
        for i in range(1, 4):
            status, _ = self.call("POST", "/api/snapshots", {
                "vehicle_id": f"AV-{i}", "model": "suv-y", "hardware_batch": "hw-a",
                "software_version": "sw-1", "battery_percent": 90,
            })
            self.assertEqual(status, 200)

        # 波次：创建 → 审批 → 启动
        status, wave = self.call("POST", "/api/waves", {
            "wave_id": "AW1", "package_id": "api-pkg", "seq": 1,
            "target_percent": 1.0, "policy_id": "api-p",
        })
        self.assertEqual(status, 200)
        self.assertEqual(wave["state"], "awaiting_approval")
        status, wave = self.call("POST", "/api/waves/AW1/approve", {"actor": "ops-wang"})
        self.assertEqual(wave["state"], "approved")
        status, wave = self.call("POST", "/api/waves/AW1/start", {"actor": "ops-wang"})
        self.assertEqual(wave["state"], "running")
        self.assertEqual(wave["members_included"], 3)

        # 入组理由可查
        status, members = self.call("GET", "/api/waves/AW1/members")
        self.assertEqual(status, 200)
        included = [m for m in members if m["included"]]
        self.assertEqual(len(included), 3)
        self.assertTrue(any("入选本波次" in r for r in included[0]["reasons"]))

        # 实时风险预算可查
        status, budget = self.call("GET", "/api/waves/AW1/risk")
        self.assertEqual(status, 200)
        self.assertEqual(budget["commands_issued"], 3)
        self.assertEqual(budget["pending"], 3)
        self.assertTrue(budget["within_budget"])

        # 上报回执（含一条重复），波次自动完结
        status, commands = self.call("GET", "/api/waves/AW1/commands")
        for i, cmd in enumerate(commands):
            status, receipt = self.call("POST", "/api/receipts", {
                "receipt_id": f"ar-{i}", "command_id": cmd["command_id"], "status": "success",
            })
            self.assertFalse(receipt["duplicate"])
        status, dup = self.call("POST", "/api/receipts", {
            "receipt_id": "ar-0", "command_id": commands[0]["command_id"], "status": "success",
        })
        self.assertTrue(dup["duplicate"])

        status, wave = self.call("GET", "/api/waves/AW1")
        self.assertEqual(wave["state"], "completed")

        # 状态迁移 / 决策 / 恢复条件均可查
        status, transitions = self.call("GET", "/api/waves/AW1/transitions")
        self.assertEqual(
            [t["to_state"] for t in transitions],
            ["awaiting_approval", "approved", "running", "completed"],
        )
        status, decisions = self.call("GET", "/api/waves/AW1/decisions")
        self.assertEqual(decisions[-1]["action"], "complete")
        self.assertEqual(decisions[-1]["policy_version"], 1)
        status, recovery = self.call("GET", "/api/waves/AW1/recovery")
        self.assertTrue(all(c["met"] for c in recovery))

        # 车辆视图
        status, vehicle = self.call("GET", "/api/vehicles/AV-1")
        self.assertEqual(vehicle["snapshot"]["hardware_batch"], "hw-a")
        self.assertEqual(len(vehicle["commands"]), 1)

    def test_error_mapping(self):
        status, body = self.call("GET", "/api/waves/no-such-wave")
        self.assertEqual(status, 404)
        status, body = self.call("POST", "/api/waves", {
            "wave_id": "BX", "package_id": "ghost", "seq": 1, "target_percent": 0.5,
        })
        self.assertEqual(status, 404)
        status, body = self.call("POST", "/api/receipts", {
            "receipt_id": "x", "command_id": "ghost", "status": "success",
        })
        self.assertEqual(status, 404)
        status, body = self.call("GET", "/api/nonexistent")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

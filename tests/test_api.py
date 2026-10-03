"""HTTP API 冒烟：运维经 API 查看波次报告。"""

import http.client
import json
import threading
import unittest

from helpers import ServiceTestCase
from rollout_control.api import make_server


class ApiTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _call(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_full_flow_over_http(self):
        status, _ = self._call("POST", "/packages", {
            "package_id": "pkg-1", "version": "sw-2.0",
            "hardware_batches": ["hw-a"], "source_versions": ["sw-1.0"],
            "min_battery_percent": 30,
        })
        self.assertEqual(status, 200)
        status, _ = self._call("POST", "/policies", {
            "version": "v1", "pause_failure_rate": 0.2,
            "rollback_failure_rate": 0.5, "batch_quarantine_rate": 0.5,
            "min_batch_sample": 2,
        })
        self.assertEqual(status, 200)
        for i in range(4):
            status, _ = self._call("POST", "/vehicles", {
                "vehicle_id": f"V-{i}", "hardware_batch": "hw-a",
                "software_version": "sw-1.0", "battery_percent": 80,
            })
            self.assertEqual(status, 200)
        status, data = self._call("POST", "/plans", {
            "package_id": "pkg-1", "policy_version": "v1",
            "wave_sizes": [0.5, 1.0], "created_by": "ops",
        })
        self.assertEqual(status, 200)
        plan_id = data["plan_id"]
        self._call("POST", f"/plans/{plan_id}/approve", {"approver": "qa"})
        status, wave = self._call("POST", f"/plans/{plan_id}/start-next-wave", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(wave["commands"]), 2)

        # 上报一条成功回执
        cmd = wave["commands"][0]
        status, receipt = self._call("POST", "/receipts", {
            "receipt_id": "r-1", "command_id": cmd["command_id"],
            "vehicle_id": cmd["vehicle_id"], "success": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(receipt["disposition"], "applied")

        # 运维查看波次报告：入组理由 / 风险预算 / 状态迁移 / 恢复条件
        status, report = self._call(
            "GET", f"/plans/{plan_id}/waves/{wave['wave_id']}/report")
        self.assertEqual(status, 200)
        self.assertTrue(report["cohort"][0]["reasons"])
        self.assertEqual(report["risk_budget"]["cohort_size"], 2)
        self.assertEqual(report["metrics"]["successes"], 1)
        self.assertTrue(report["transitions"])
        self.assertTrue(any(c["id"] == "manual_resume"
                            for c in report["recovery_conditions"]))

        status, decisions = self._call("GET", f"/plans/{plan_id}/decisions")
        self.assertEqual(status, 200)
        self.assertTrue(decisions)
        status, audit = self._call("GET", f"/audit?plan_id={plan_id}")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["type"] == "plan_approved" for e in audit))

    def test_error_mapping(self):
        status, data = self._call("GET", "/plans/no-such-plan")
        self.assertEqual(status, 404)
        self.assertIn("error", data)
        status, _ = self._call("POST", "/plans", {"package_id": "x"})
        self.assertEqual(status, 422)
        status, _ = self._call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

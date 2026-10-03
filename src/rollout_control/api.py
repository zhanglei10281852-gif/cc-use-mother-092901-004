"""运维 HTTP JSON API（仅依赖标准库）。

运维人员可经由本 API 查看任一波次的入组理由、实时风险预算、
状态迁移与恢复条件，并执行审批、暂停、恢复、回滚、隔离等操作。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .contracts import Compatibility, RiskPolicy, SoftwarePackage, VehicleSnapshot
from .service import RolloutControlService, ServiceError

Handler = Callable[[dict, Optional[dict]], Any]


class RolloutApi:
    """路由层：handle() 可直接被测试调用，也可由 HTTP 服务器驱动。"""

    def __init__(self, service: RolloutControlService):
        self.service = service
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = [
            ("GET", re.compile(r"^/api/health$"), lambda p, b: {"status": "ok"}),
            # 查询
            ("GET", re.compile(r"^/api/packages$"), lambda p, b: self.service.list_packages()),
            ("GET", re.compile(r"^/api/packages/(?P<package_id>[^/]+)$"),
             lambda p, b: self.service.get_package(p["package_id"])),
            ("GET", re.compile(r"^/api/policies$"), lambda p, b: self.service.list_policies()),
            ("GET", re.compile(r"^/api/waves$"), lambda p, b: self.service.list_waves()),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)$"),
             lambda p, b: self.service.get_wave(p["wave_id"])),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/members$"),
             lambda p, b: self.service.get_members(p["wave_id"])),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/risk$"),
             lambda p, b: self.service.get_risk_budget(p["wave_id"])),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/transitions$"),
             lambda p, b: self.service.get_transitions(p["wave_id"])),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/decisions$"),
             lambda p, b: self.service.get_decisions(p["wave_id"])),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/recovery$"),
             lambda p, b: self.service.get_recovery_conditions(p["wave_id"])),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/commands$"),
             lambda p, b: self.service.list_commands(p["wave_id"])),
            ("GET", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/receipts$"),
             lambda p, b: self.service.get_receipts(p["wave_id"])),
            ("GET", re.compile(r"^/api/vehicles/(?P<vehicle_id>[^/]+)$"),
             lambda p, b: self.service.get_vehicle(p["vehicle_id"])),
            ("GET", re.compile(r"^/api/anomalies$"), lambda p, b: self.service.list_anomalies()),
            # 登记
            ("POST", re.compile(r"^/api/packages$"), self._create_package),
            ("POST", re.compile(r"^/api/policies$"), self._create_policy),
            ("POST", re.compile(r"^/api/snapshots$"), self._create_snapshot),
            ("POST", re.compile(r"^/api/waves$"), self._create_wave),
            # 波次操作
            ("POST", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/approve$"),
             lambda p, b: self.service.approve_wave(p["wave_id"], **self._kwargs(b, "actor"))),
            ("POST", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/start$"),
             lambda p, b: self.service.start_wave(p["wave_id"], **self._kwargs(b, "actor"))),
            ("POST", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/pause$"),
             lambda p, b: self.service.pause_wave(p["wave_id"], **self._kwargs(b, "actor", "reason"))),
            ("POST", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/resume$"),
             lambda p, b: self.service.resume_wave(p["wave_id"], **self._kwargs(b, "actor", "override_reason"))),
            ("POST", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/rollback$"),
             lambda p, b: self.service.rollback_wave(p["wave_id"], **self._kwargs(b, "actor", "reason"))),
            ("POST", re.compile(r"^/api/waves/(?P<wave_id>[^/]+)/quarantine$"),
             lambda p, b: self.service.quarantine_wave(p["wave_id"], **self._kwargs(b, "actor", "reason"))),
            # 信号接入
            ("POST", re.compile(r"^/api/receipts$"), self._create_receipt),
            ("POST", re.compile(r"^/api/incidents$"), self._create_incident),
            ("POST", re.compile(r"^/api/health-reports$"), self._create_health_report),
            ("POST", re.compile(r"^/api/vehicles/(?P<vehicle_id>[^/]+)/quarantine$"),
             lambda p, b: self.service.quarantine_vehicle(
                 p["vehicle_id"], **self._kwargs(b, "reason", "actor"))),
        ]

    @staticmethod
    def _kwargs(body: Optional[dict], *keys: str) -> dict:
        body = body or {}
        return {k: body[k] for k in keys if k in body}

    def _create_package(self, p: dict, b: Optional[dict]) -> Any:
        b = b or {}
        compat = Compatibility(**b.get("compatibility", {}))
        return self.service.register_package(
            SoftwarePackage(
                package_id=b["package_id"],
                model=b["model"],
                version=b["version"],
                compatibility=compat,
            )
        )

    def _create_policy(self, p: dict, b: Optional[dict]) -> Any:
        return self.service.register_policy(RiskPolicy(**(b or {})))

    def _create_snapshot(self, p: dict, b: Optional[dict]) -> Any:
        return self.service.register_snapshot(VehicleSnapshot(**(b or {})))

    def _create_wave(self, p: dict, b: Optional[dict]) -> Any:
        b = b or {}
        return self.service.create_wave(
            wave_id=b["wave_id"],
            package_id=b["package_id"],
            seq=b["seq"],
            target_percent=b["target_percent"],
            **self._kwargs(b, "policy_id", "actor"),
        )

    def _create_receipt(self, p: dict, b: Optional[dict]) -> Any:
        b = b or {}
        return self.service.submit_receipt(
            receipt_id=b["receipt_id"],
            command_id=b["command_id"],
            status=b["status"],
            **self._kwargs(b, "detail", "reported_at"),
        )

    def _create_incident(self, p: dict, b: Optional[dict]) -> Any:
        b = b or {}
        return self.service.report_incident(
            incident_id=b["incident_id"],
            vehicle_id=b["vehicle_id"],
            severity=b["severity"],
            **self._kwargs(b, "summary", "reported_at"),
        )

    def _create_health_report(self, p: dict, b: Optional[dict]) -> Any:
        b = b or {}
        return self.service.report_health(
            vehicle_id=b["vehicle_id"],
            score=b["score"],
            **self._kwargs(b, "reported_at"),
        )

    def handle(self, method: str, path: str, body: Optional[dict]) -> tuple[int, Any]:
        for route_method, pattern, handler in self._routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                result = handler(match.groupdict(), body)
                return 200, self._to_jsonable(result)
        return 404, {"error": f"路径不存在：{method} {path}"}

    @classmethod
    def _to_jsonable(cls, obj: Any) -> Any:
        if is_dataclass(obj) and not isinstance(obj, type):
            return {k: cls._to_jsonable(v) for k, v in asdict(obj).items()}
        if isinstance(obj, list):
            return [cls._to_jsonable(v) for v in obj]
        if isinstance(obj, tuple):
            return [cls._to_jsonable(v) for v in obj]
        if isinstance(obj, dict):
            return {k: cls._to_jsonable(v) for k, v in obj.items()}
        return obj


def make_server(
    service: RolloutControlService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    api = RolloutApi(service)

    class RequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body: Optional[dict] = None
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    self._respond(400, {"error": "请求体不是合法 JSON"})
                    return
            path = self.path.split("?", 1)[0]
            try:
                status, payload = api.handle(method, path, body)
            except ServiceError as exc:
                status, payload = exc.status_code, {"error": str(exc)}
            except KeyError as exc:
                status, payload = 400, {"error": f"缺少必填字段：{exc}"}
            except (ValueError, TypeError) as exc:
                status, payload = 400, {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
            self._respond(status, payload)

        def _respond(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, *args: Any) -> None:  # 静默访问日志
            pass

    return ThreadingHTTPServer((host, port), RequestHandler)

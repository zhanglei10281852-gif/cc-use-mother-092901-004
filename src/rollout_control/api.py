"""运维 HTTP API（仅标准库）。

运行方式：
    python -m rollout_control.api --data-dir ./data --port 8080

所有端点以 JSON 交互；错误返回 {"error": 消息} 与对应状态码。
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .contracts import (
    Compatibility,
    HealthReport,
    IncidentReport,
    InstallReceipt,
    RiskPolicy,
    Severity,
    SoftwarePackage,
    VehicleSnapshot,
)
from .service import RolloutControlService, ServiceError

Handler = Callable[["Request", re.Match], object]


class Request:
    def __init__(self, body: dict, query: dict[str, list[str]]) -> None:
        self.body = body
        self.query = query


def _routes(service: RolloutControlService) -> list[tuple[str, re.Pattern, Handler]]:
    def route(method: str, pattern: str):
        def decorator(fn: Handler) -> Handler:
            handlers.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return decorator

    handlers: list[tuple[str, re.Pattern, Handler]] = []

    @route("POST", r"/packages")
    def register_package(req: Request, m: re.Match) -> dict:
        b = req.body
        compat = Compatibility(
            hardware_batches=frozenset(b.get("hardware_batches", [])),
            source_versions=frozenset(b.get("source_versions", [])),
            min_battery_percent=b.get("min_battery_percent", 0),
            require_online=b.get("require_online", True),
            models=frozenset(b.get("models", [])),
        )
        package = SoftwarePackage(
            package_id=b["package_id"], version=b["version"],
            compatibility=compat, description=b.get("description", ""))
        service.register_package(package)
        return {"package_id": package.package_id}

    @route("POST", r"/policies")
    def register_policy(req: Request, m: re.Match) -> dict:
        policy = RiskPolicy(**req.body)
        service.register_policy(policy)
        return {"version": policy.version}

    @route("POST", r"/vehicles")
    def register_vehicle(req: Request, m: re.Match) -> dict:
        snapshot = VehicleSnapshot(**req.body)
        service.register_vehicle(snapshot)
        return {"vehicle_id": snapshot.vehicle_id}

    @route("POST", r"/plans")
    def create_plan(req: Request, m: re.Match) -> dict:
        b = req.body
        plan_id = service.create_plan(
            package_id=b["package_id"],
            policy_version=b["policy_version"],
            wave_sizes=b["wave_sizes"],
            created_by=b.get("created_by", "api"),
            auto_promote=b.get("auto_promote", True),
            plan_id=b.get("plan_id"),
        )
        return {"plan_id": plan_id}

    @route("GET", r"/plans/(?P<plan_id>[^/]+)")
    def get_plan(req: Request, m: re.Match) -> dict:
        return service.get_plan(m["plan_id"])

    @route("POST", r"/plans/(?P<plan_id>[^/]+)/approve")
    def approve_plan(req: Request, m: re.Match) -> dict:
        service.approve_plan(m["plan_id"], req.body.get("approver", "api"))
        return {"plan_id": m["plan_id"], "state": "approved"}

    @route("POST", r"/plans/(?P<plan_id>[^/]+)/start-next-wave")
    def start_next_wave(req: Request, m: re.Match) -> dict:
        return service.start_next_wave(m["plan_id"])

    @route("POST", r"/plans/(?P<plan_id>[^/]+)/evaluate")
    def evaluate(req: Request, m: re.Match) -> dict:
        decision = service.evaluate_plan(m["plan_id"])
        return {"decision": None if decision is None else decision.to_dict()}

    @route("POST", r"/plans/(?P<plan_id>[^/]+)/resume")
    def resume(req: Request, m: re.Match) -> dict:
        service.resume_plan(m["plan_id"], req.body.get("operator", "api"))
        return {"plan_id": m["plan_id"], "state": "running"}

    @route("POST", r"/plans/(?P<plan_id>[^/]+)/rollback")
    def rollback(req: Request, m: re.Match) -> dict:
        service.rollback_plan(m["plan_id"], req.body.get("reason", "手动回滚"),
                              req.body.get("operator", "api"))
        return {"plan_id": m["plan_id"], "state": "rolled_back"}

    @route("POST", r"/plans/(?P<plan_id>[^/]+)/policy-migration")
    def migrate_policy(req: Request, m: re.Match) -> dict:
        service.migrate_plan_policy(m["plan_id"], req.body["new_version"],
                                    req.body.get("reason", ""))
        return {"plan_id": m["plan_id"], "policy_version": req.body["new_version"]}

    @route("GET", r"/plans/(?P<plan_id>[^/]+)/waves/(?P<wave_id>[^/]+)/report")
    def wave_report(req: Request, m: re.Match) -> dict:
        return service.wave_report(m["plan_id"], m["wave_id"])

    @route("GET", r"/plans/(?P<plan_id>[^/]+)/decisions")
    def decisions(req: Request, m: re.Match) -> list:
        return service.list_decisions(m["plan_id"])

    @route("POST", r"/plans/(?P<plan_id>[^/]+)/waves/(?P<wave_id>[^/]+)/retry")
    def retry(req: Request, m: re.Match) -> dict:
        command = service.retry_vehicle(m["plan_id"], m["wave_id"],
                                        req.body["vehicle_id"])
        return command.to_dict()

    @route("POST", r"/receipts")
    def receipt(req: Request, m: re.Match) -> dict:
        record = service.ingest_receipt(InstallReceipt(**req.body))
        return record.to_dict()

    @route("POST", r"/health")
    def health(req: Request, m: re.Match) -> dict:
        report = HealthReport(
            vehicle_id=req.body["vehicle_id"],
            health_score=req.body["health_score"],
            reported_at=req.body.get("reported_at", 0.0),
            fault_codes=tuple(req.body.get("fault_codes", ())),
        )
        service.ingest_health(report)
        return {"vehicle_id": report.vehicle_id}

    @route("POST", r"/incidents")
    def incident(req: Request, m: re.Match) -> dict:
        b = dict(req.body)
        b["severity"] = Severity(b["severity"])
        service.file_incident(IncidentReport(**b))
        return {"incident_id": req.body["incident_id"]}

    @route("POST", r"/incidents/(?P<incident_id>[^/]+)/resolve")
    def resolve_incident(req: Request, m: re.Match) -> dict:
        service.resolve_incident(m["incident_id"])
        return {"incident_id": m["incident_id"], "resolved": True}

    @route("POST", r"/quarantines/(?P<batch>[^/]+)/clear")
    def clear_quarantine(req: Request, m: re.Match) -> dict:
        service.clear_quarantine(m["batch"], req.body.get("operator", "api"))
        return {"hardware_batch": m["batch"], "cleared": True}

    @route("GET", r"/vehicles/(?P<vehicle_id>[^/]+)")
    def get_vehicle(req: Request, m: re.Match) -> dict:
        return service.get_vehicle(m["vehicle_id"])

    @route("GET", r"/audit")
    def audit(req: Request, m: re.Match) -> list:
        plan_id = req.query.get("plan_id", [None])[0]
        limit = int(req.query.get("limit", ["200"])[0])
        return service.audit_log(plan_id=plan_id, limit=limit)

    return handlers


def make_server(service: RolloutControlService, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    routes = _routes(service)

    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # 静默访问日志
            pass

        def _handle(self, method: str) -> None:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            body: dict = {}
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    raw = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(raw, dict):
                        raise ServiceError("请求体必须是 JSON 对象")
                    body = raw
            except json.JSONDecodeError:
                self._send(400, {"error": "请求体不是合法 JSON"})
                return
            for route_method, pattern, handler in routes:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if match is None:
                    continue
                try:
                    result = handler(Request(body, query), match)
                    self._send(200, result)
                except ServiceError as exc:
                    self._send(exc.status, {"error": str(exc)})
                except (KeyError, TypeError, ValueError) as exc:
                    self._send(422, {"error": f"请求参数不合法: {exc}"})
                return
            self._send(404, {"error": f"路径不存在: {method} {parsed.path}"})

        def _send(self, status: int, payload: object) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

    return ThreadingHTTPServer((host, port), ApiHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="车端软件灰度发布控制服务")
    parser.add_argument("--data-dir", default="./rollout-data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    service = RolloutControlService(args.data_dir)
    server = make_server(service, args.host, args.port)
    print(f"灰度发布控制服务已启动: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

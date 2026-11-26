"""Standard-library HTTP/JSON adapter for the turnover-box service."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from .service import TurnBoxService

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def create_handler(service: TurnBoxService, lock: RLock | None = None):
    lock = lock or RLock()

    class TurnBoxHandler(BaseHTTPRequestHandler):
        server_version = "TurnBoxService/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter tests/demo
            if getattr(self.server, "verbose", False):
                super().log_message(fmt, *args)

        # ------------------------------------------------------------------
        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Actor,X-Role")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, path: Path) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("request body must be UTF-8 JSON") from exc

        def _save(self) -> None:
            if service.state_path:
                service.save()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Actor,X-Role")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()

        # ------------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = [p for p in parsed.path.split("/") if p]
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                with lock:
                    self._route_get(path, query)
            except NotFoundError as exc:
                self._send_json(404, {"error": "not_found", "message": str(exc)})
            except ValidationError as exc:
                self._send_json(400, {"error": "validation_error", "message": str(exc)})
            except (ConflictError, BusinessRuleError) as exc:
                code = 409 if isinstance(exc, ConflictError) else 422
                self._send_json(code, {"error": type(exc).__name__, "message": str(exc)})
            except Exception as exc:  # pragma: no cover - boundary guard
                self._send_json(500, {"error": "internal_error", "message": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = [p for p in parsed.path.split("/") if p]
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                payload = self._read_json()
                with lock:
                    result, status = self._route_post(path, query, payload)
                    self._save()
                self._send_json(status, result)
            except NotFoundError as exc:
                self._send_json(404, {"error": "not_found", "message": str(exc)})
            except ValidationError as exc:
                self._send_json(400, {"error": "validation_error", "message": str(exc)})
            except ConflictError as exc:
                self._send_json(409, {"error": "conflict", "message": str(exc)})
            except BusinessRuleError as exc:
                self._send_json(422, {"error": "business_rule_error", "message": str(exc)})
            except Exception as exc:  # pragma: no cover - boundary guard
                self._send_json(500, {"error": "internal_error", "message": str(exc)})

        # ------------------------------------------------------------------
        def _route_get(self, path: list[str], query: dict[str, str]) -> None:
            if not path or path == ["index.html"]:
                index = STATIC_DIR / "index.html"
                if index.exists():
                    self._send_html(index)
                else:
                    self._send_json(200, self._service_description())
                return
            if path in (["health"], ["healthz"]) or path == ["api", "health"]:
                self._send_json(200, {"status": "ok", "service": "turnbox"})
                return
            if path == ["api"] or path == ["api", "docs"]:
                self._send_json(200, self._service_description())
                return

            if path[:2] == ["api", "customer"] or path[:2] == ["api", "customers"]:
                cid = path[2] if len(path) >= 3 else query.get("id") or query.get("customer")
                if not cid:
                    raise ValidationError("customer id is required")
                self._send_json(200, service.customer_view(cid))
                return

            if path[:2] in (["api", "merchant"], ["api", "merchants"], ["api", "store"], ["api", "stores"]):
                mid = path[2] if len(path) >= 3 else query.get("id") or query.get("merchant") or query.get("store")
                if not mid:
                    raise ValidationError("merchant/store id is required")
                self._send_json(200, service.merchant_view(mid, query.get("period")))
                return

            if path[:2] == ["api", "issue"] or path[:2] == ["api", "issues"]:
                iid = path[2] if len(path) >= 3 else query.get("id") or query.get("issue")
                if not iid:
                    raise ValidationError("issue id is required")
                view = service.issue_view(iid)
                if len(path) >= 4 and path[3] == "business":
                    view = {
                        "issue": iid,
                        "business": view["accounts"],
                        "deposit": view["deposit"],
                        "refund": view["refund"],
                        "service_fee": view["service_fee"],
                    }
                self._send_json(200, view)
                return

            if path[:2] == ["api", "box"] or path[:2] == ["api", "boxes"]:
                bid = path[2] if len(path) >= 3 else query.get("id") or query.get("box")
                if not bid:
                    raise ValidationError("box id is required")
                view = service.box_view(bid)
                if len(path) >= 4 and path[3] == "trace":
                    pass  # full view already contains timeline
                self._send_json(200, view)
                return

            if path == ["api", "points"]:
                self._send_json(200, {"points": list(service.points.values())})
                return
            if len(path) == 3 and path[:2] == ["api", "points"]:
                self._send_json(200, service.point_view(path[2]))
                return
            if path[:2] == ["api", "return-points"] and len(path) == 3:
                self._send_json(200, service.point_view(path[2]))
                return

            if path == ["api", "disputes"] or path == ["api", "conflicts"] or path == ["api", "admin", "disputes"]:
                self._send_json(200, {"disputes": service.list_disputes(), "conflicts": service.list_disputes()})
                return
            if len(path) == 3 and path[0] == "api" and path[1] in {"disputes", "conflicts"}:
                self._send_json(200, service.get_dispute(path[2]))
                return
            if len(path) == 4 and path[:3] == ["api", "admin", "disputes"]:
                self._send_json(200, service.get_dispute(path[3]))
                return

            if path == ["api", "admin"] or path == ["api", "admin", "overview"]:
                self._send_json(200, service.admin_overview())
                return
            if path == ["api", "admin", "integrity"]:
                self._send_json(200, service.integrity_report())
                return

            if path == ["api", "settlements"]:
                period = query.get("period")
                items = service.list_settlements()
                if period:
                    items = [s for s in items if s.get("period") == period]
                self._send_json(200, {"settlements": items})
                return
            if len(path) == 3 and path[:2] == ["api", "settlements"]:
                self._send_json(200, service.settlements[path[2]])
                return
            if path == ["api", "reports", "monthly"] or path == ["api", "monthly-report"]:
                period = query.get("period")
                if not period:
                    raise ValidationError("period=YYYY-MM is required")
                self._send_json(
                    200,
                    {"period": period, "lines": service.settlement_lines(period)},
                )
                return

            self._send_json(404, {"error": "not_found", "path": "/" + "/".join(path)})

        def _route_post(self, path: list[str], query: dict[str, str], payload: Any):
            if not isinstance(payload, dict):
                raise ValidationError("JSON request body must be an object")

            if path == ["api", "records"]:
                record = payload.get("record", payload)
                typ = payload.get("type") or payload.get("record_type")
                return service.ingest(record, typ), 201

            for typ in ("policies", "points", "collections"):
                if path == ["api", typ]:
                    return service.ingest(payload, typ), 201

            if path == ["api", "refunds"]:
                if payload.get("channel_ref") and payload.get("state") and payload.get("occurred_at"):
                    return service.ingest(payload, "refunds"), 201
                issue_id = payload.get("issue") or payload.get("issue_id")
                if not issue_id:
                    raise ValidationError("refund creation requires issue/issue_id")
                return service.create_refund(issue_id, payload), 201

            if path == ["api", "ingest", "ndjson"]:
                text = payload.get("ndjson") or payload.get("text") or ""
                typ = payload.get("type") or query.get("type")
                return service.ingest_ndjson(text, typ), 201

            if path == ["api", "issues"]:
                return service.create_issue(payload), 201
            if path == ["api", "orders"]:
                return service.create_order(payload), 201
            if path == ["api", "moves"]:
                return service.record_move(payload), 201

            if len(path) == 4 and path[:2] == ["api", "issues"] and path[3] == "refunds":
                return service.create_refund(path[2], payload), 201
            if len(path) == 4 and path[:2] == ["api", "issues"] and path[3] in {"entries", "adjustments", "objections"}:
                body = dict(payload)
                if path[3] == "objections":
                    body.setdefault("kind", "objection")
                return service.add_issue_entry(path[2], body), 201

            if len(path) >= 3 and path[:2] == ["api", "refunds"]:
                ref = path[2]
                if len(path) == 4 and path[3] in {"accept", "settle", "fail", "reject"}:
                    state = {"accept": "accepted", "settle": "settled", "fail": "failed", "reject": "rejected"}[path[3]]
                    return service.transition_refund(ref, state, payload), 200
                if len(path) == 4 and path[3] == "transition":
                    return service.transition_refund(ref, payload.get("state", "accepted"), payload), 200

            if path == ["api", "disputes", "resolve"]:
                did = payload.get("dispute_id") or query.get("id")
                return service.resolve_dispute(did, payload), 200
            if len(path) == 4 and path[:2] == ["api", "disputes"] and path[3] == "resolve":
                return service.resolve_dispute(path[2], payload), 200
            if len(path) == 4 and path[:2] == ["api", "conflicts"] and path[3] == "resolve":
                return service.resolve_dispute(path[2], payload), 200
            if len(path) == 5 and path[:3] == ["api", "admin", "disputes"] and path[4] == "resolve":
                return service.resolve_dispute(path[3], payload), 200

            if path == ["api", "settlements"]:
                period = payload.get("period") or query.get("period")
                return service.create_settlement(period, payload), 201
            if len(path) == 4 and path[:3] == ["api", "admin", "settlements"] and path[3] == "finalize":
                period = path[2]
                return service.settle_month(period, payload.get("at")), 201
            if len(path) == 4 and path[:2] == ["api", "settlements"] and path[3] == "correct":
                return service.correct_settlement(path[2], payload), 201

            if path == ["api", "save"]:
                p = service.save(payload.get("path"))
                return {"status": "saved", "path": str(p)}, 200

            raise NotFoundError("unknown POST endpoint: /" + "/".join(path))

        @staticmethod
        def _service_description() -> dict[str, Any]:
            return {
                "service": "turnbox",
                "version": "1.0.0",
                "endpoints": [
                    "GET /api/customers/{customer}",
                    "GET /api/merchants/{merchant}",
                    "GET /api/issues/{issue}",
                    "GET /api/issues/{issue}/business",
                    "GET /api/boxes/{box}",
                    "GET /api/disputes",
                    "POST /api/disputes/{id}/resolve",
                    "GET /api/admin/overview",
                    "POST /api/issues/{issue}/refunds",
                    "POST /api/refunds/{channel_ref}/settle",
                    "GET /api/settlements?period=YYYY-MM",
                    "POST /api/settlements",
                    "POST /api/settlements/{id}/correct",
                    "POST /api/records",
                    "POST /api/orders",
                    "POST /api/moves",
                ],
            }

    return TurnBoxHandler


def build_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    db_path: str | Path | None = None,
    load_samples: bool = False,
) -> ThreadingHTTPServer:
    service = TurnBoxService.load(db_path) if db_path else TurnBoxService()
    if load_samples and not service.issues:
        sample_dir = Path(__file__).resolve().parents[1] / "samples"
        service.load_directory(sample_dir)
        if db_path:
            service.save(db_path)
    handler = create_handler(service)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def serve(
    host: str = "0.0.0.0",
    port: int = 8000,
    db_path: str | Path | None = None,
    load_samples: bool = False,
) -> None:
    httpd = build_server(host, port, db_path, load_samples)
    print(f"TurnBox service listening on http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

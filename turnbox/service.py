"""Core business service for shared turnover boxes.

The service is intentionally framework-free.  HTTP/CLI adapters live beside it,
and tests or other Python systems can use :class:`TurnBoxService` directly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from .errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError


SEVEN_DAYS = timedelta(days=7)
CURRENCY = "CNY_CENT"
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

STATUS_ZH = {
    "unknown": "未知",
    "available": "清洗验收通过、可借",
    "rented": "租用中",
    "with_customer": "顾客/代送人保管中",
    "at_machine_pending_check": "归还机已暂收，待开箱点验",
    "pending_cleaning": "已点验/交接，待清洗或清洗中",
    "in_transit": "交接在途",
    "wash_failed": "清洗不合格，待维修或淘汰",
}


def parse_dt(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValidationError("occurred_at/issued_at must be an ISO-8601 string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        # The exchange format uses explicit offsets.  Be permissive for local
        # tests while ensuring aware/naive values can never raise TypeError.
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def collection_ref_key(point: Any, event: Any) -> str:
    return json.dumps([point, event], ensure_ascii=False, separators=(",", ":"))


def normalize_ref(ref: Any) -> tuple[Any, Any]:
    if isinstance(ref, Mapping):
        if "point" in ref and ("event" in ref or "event_id" in ref):
            return ref["point"], ref.get("event", ref.get("event_id"))
        if len(ref) == 2:
            vals = list(ref.values())
            return vals[0], vals[1]
    if isinstance(ref, (list, tuple)) and len(ref) == 2:
        return ref[0], ref[1]
    if isinstance(ref, str) and ("/" in ref or ":" in ref):
        sep = "/" if "/" in ref else ":"
        point, event = ref.split(sep, 1)
        return point, event
    raise ValidationError("reference must be [point, event], {'point':...,'event':...}, or point/event")


def _require(record: Mapping[str, Any], fields: list[str], kind: str) -> None:
    missing = [name for name in fields if name not in record or record[name] in (None, "")]
    if missing:
        raise ValidationError(f"{kind} record missing required field(s): {', '.join(missing)}")


def _infer_type(record: Mapping[str, Any]) -> str | None:
    if record.get("record_type") or record.get("_type"):
        return record.get("record_type") or record.get("_type")
    if "policy" in record and "deduction" in record:
        return "policies"
    if "issued_at" in record and "issue" in record:
        return "issues"
    if "channel_ref" in record:
        return "refunds"
    if "move" in record and "kind" in record:
        return "moves"
    if "kind" in record and record.get("kind") in {"machine_in", "box_check", "confirmed"}:
        return "collections"
    if "point" in record and "merchant" in record:
        return "points"
    return None


class TurnBoxService:
    """Event-sourced-ish service over NDJSON exchange records and actions."""

    RECORD_TYPES = {"policies", "points", "issues", "collections", "moves", "refunds"}
    FILE_TYPES = {
        "policies": "policies",
        "policy": "policies",
        "points": "points",
        "point": "points",
        "issues": "issues",
        "issue": "issues",
        "collections": "collections",
        "collection": "collections",
        "moves": "moves",
        "move": "moves",
        "refunds": "refunds",
        "refund": "refunds",
    }

    def __init__(self, state: Mapping[str, Any] | None = None):
        self.policies: dict[str, dict[str, Any]] = {}
        self.points: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, dict[str, Any]] = {}
        self.collections: list[dict[str, Any]] = []
        self._collection_index: dict[str, dict[str, Any]] = {}
        self.moves: dict[str, dict[str, Any]] = {}
        self.move_order: list[str] = []
        self.refunds: list[dict[str, Any]] = []
        self.issue_entries: dict[str, list[dict[str, Any]]] = {}
        self.dispute_resolutions: dict[str, dict[str, Any]] = {}
        self.settlements: dict[str, dict[str, Any]] = {}
        self.settlement_order: list[str] = []
        self.adjustments: dict[str, dict[str, Any]] = {}
        self.adjustment_order: list[str] = []
        self.counters: dict[str, int] = {"refund": 0, "adjustment": 0, "order": 0, "issue_entry": 0, **(state.get("counters", {}) if state else {})}
        self.state_path: str | Path | None = None
        if state:
            self.load_state(state)

    # ------------------------------------------------------------------
    # Persistence and NDJSON exchange
    # ------------------------------------------------------------------
    def load_state(self, state: Mapping[str, Any]) -> None:
        self.policies = dict(state.get("policies", {}))
        self.points = dict(state.get("points", {}))
        self.issues = dict(state.get("issues", {}))
        self.collections = list(state.get("collections", []))
        self._collection_index = {
            collection_ref_key(r["point"], r["event"]): r for r in self.collections
        }
        self.moves = dict(state.get("moves", {}))
        self.move_order = list(state.get("move_order", list(self.moves)))
        self.refunds = list(state.get("refunds", []))
        self.issue_entries = dict(state.get("issue_entries", {}))
        self.dispute_resolutions = dict(state.get("dispute_resolutions", {}))
        self.settlements = dict(state.get("settlements", {}))
        self.settlement_order = list(state.get("settlement_order", list(self.settlements)))
        self.adjustments = dict(state.get("adjustments", {}))
        self.adjustment_order = list(state.get("adjustment_order", list(self.adjustments)))
        self.counters = {
            "refund": 0,
            "adjustment": 0,
            "order": 0,
            "issue_entry": 0,
            **state.get("counters", {}),
        }

    def to_state(self) -> dict[str, Any]:
        return {
            "format": "turnbox-state",
            "version": 1,
            "policies": self.policies,
            "points": self.points,
            "issues": self.issues,
            "collections": self.collections,
            "moves": self.moves,
            "move_order": self.move_order,
            "refunds": self.refunds,
            "issue_entries": self.issue_entries,
            "dispute_resolutions": self.dispute_resolutions,
            "settlements": self.settlements,
            "settlement_order": self.settlement_order,
            "adjustments": self.adjustments,
            "adjustment_order": self.adjustment_order,
            "counters": self.counters,
        }

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path or self.state_path or "turnbox-state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_state(), fh, ensure_ascii=False, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        self.state_path = path
        return path

    @classmethod
    def load(cls, path: str | Path) -> "TurnBoxService":
        p = Path(path)
        if not p.exists():
            svc = cls()
            svc.state_path = p
            return svc
        data = json.loads(p.read_text(encoding="utf-8"))
        svc = cls(data)
        svc.state_path = p
        return svc

    @classmethod
    def open(cls, path: str | Path | None = None) -> "TurnBoxService":
        return cls.load(path) if path else cls()

    def ingest(
        self,
        record: Mapping[str, Any],
        record_type: str | None = None,
        *,
        source: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise ValidationError("each exchange record must be a JSON object")
        typ = self.FILE_TYPES.get(record_type or "", record_type or "") or _infer_type(record)
        if typ not in self.RECORD_TYPES:
            raise ValidationError(f"unknown record type: {record_type!r}")
        rec = dict(record)
        if source:
            rec.setdefault("_source", source)

        if typ == "policies":
            _require(rec, ["policy", "deduction", "collection_fee"], typ)
            key = str(rec["policy"])
            self._put_unique(self.policies, key, rec, "policy")
        elif typ == "points":
            _require(rec, ["point", "merchant"], typ)
            key = str(rec["point"])
            self._put_unique(self.points, key, rec, "point")
        elif typ == "issues":
            _require(
                rec,
                ["issue", "box", "merchant", "customer", "policy", "deposit", "issued_at"],
                typ,
            )
            parse_dt(rec["issued_at"])
            if not isinstance(rec["deposit"], int) or rec["deposit"] < 0:
                raise ValidationError("deposit must be a non-negative integer number of cents")
            key = str(rec["issue"])
            self._put_unique(self.issues, key, rec, "issue")
        elif typ == "collections":
            _require(rec, ["point", "event", "kind", "occurred_at"], typ)
            if rec["kind"] not in {"machine_in", "box_check", "confirmed"}:
                raise ValidationError(f"unsupported collection kind: {rec['kind']!r}")
            parse_dt(rec["occurred_at"])
            key = collection_ref_key(rec["point"], rec["event"])
            existing = self._collection_index.get(key)
            if existing is not None:
                if existing != rec:
                    raise ConflictError(
                        f"collection identity {rec['point']}/{rec['event']} already has different content"
                    )
                return {"status": "duplicate", "type": typ, "id": [rec["point"], rec["event"]]}
            self._collection_index[key] = rec
            self.collections.append(rec)
            return {"status": "inserted", "type": typ, "id": [rec["point"], rec["event"]]}
        elif typ == "moves":
            _require(rec, ["move", "box", "kind", "occurred_at", "from", "to"], typ)
            parse_dt(rec["occurred_at"])
            key = str(rec["move"])
            if key in self.moves:
                if self.moves[key] != rec:
                    raise ConflictError(f"move {key!r} already has different content")
                return {"status": "duplicate", "type": typ, "id": key}
            self.moves[key] = rec
            self.move_order.append(key)
            return {"status": "inserted", "type": typ, "id": key}
        else:  # refunds
            _require(rec, ["channel_ref", "issue", "amount", "state", "occurred_at"], typ)
            parse_dt(rec["occurred_at"])
            for old in self.refunds:
                same_event = (
                    old.get("channel_ref") == rec["channel_ref"]
                    and old.get("state") == rec["state"]
                    and old.get("occurred_at") == rec["occurred_at"]
                )
                if same_event:
                    if old != rec:
                        raise ConflictError(
                            f"refund event {rec['channel_ref']} {rec['state']} at {rec['occurred_at']} "
                            "has different content"
                        )
                    return {"status": "duplicate", "type": typ, "id": rec["channel_ref"]}
            self.refunds.append(rec)
            return {"status": "inserted", "type": typ, "id": rec["channel_ref"]}

        return {"status": "inserted", "type": typ, "id": key}

    @staticmethod
    def _put_unique(store: dict[str, dict[str, Any]], key: str, rec: dict[str, Any], label: str) -> None:
        if key in store:
            if store[key] != rec:
                raise ConflictError(f"{label} {key!r} already exists with different content")
            return
        store[key] = rec

    def ingest_many(self, records: list[Mapping[str, Any]], record_type: str | None = None) -> dict[str, Any]:
        results = []
        counts = {"inserted": 0, "duplicate": 0}
        for record in records:
            result = self.ingest(record, record_type)
            results.append(result)
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        return {"counts": counts, "results": results}

    def ingest_ndjson(self, text: str, record_type: str | None = None) -> dict[str, Any]:
        records = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"NDJSON line {lineno} is not valid JSON: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValidationError(f"NDJSON line {lineno} must be a JSON object")
            records.append(value)
        return self.ingest_many(records, record_type)

    def ingest_file(self, path: str | Path, record_type: str | None = None) -> dict[str, Any]:
        p = Path(path)
        typ = record_type or self.FILE_TYPES.get(p.stem, p.stem)
        return self.ingest_ndjson(p.read_text(encoding="utf-8"), typ)

    def load_directory(self, directory: str | Path) -> dict[str, Any]:
        root = Path(directory)
        if not root.is_dir():
            raise NotFoundError(f"exchange directory not found: {root}")
        summary: dict[str, Any] = {"files": [], "inserted": 0, "duplicate": 0}
        for path in sorted(root.glob("*.ndjson")):
            result = self.ingest_file(path)
            item = {"file": str(path), **result["counts"]}
            summary["files"].append(item)
            summary["inserted"] += result["counts"].get("inserted", 0)
            summary["duplicate"] += result["counts"].get("duplicate", 0)
        return summary

    # ------------------------------------------------------------------
    # Basic lookups and collection verification
    # ------------------------------------------------------------------
    def get_issue(self, issue_id: str) -> dict[str, Any]:
        if issue_id not in self.issues:
            raise NotFoundError(f"unknown issue/lease: {issue_id}")
        return self.issues[issue_id]

    def get_box_records(self, box_id: str) -> dict[str, list[dict[str, Any]]]:
        return {
            "issues": [r for r in self.issues.values() if r.get("box") == box_id],
            "collections": [r for r in self.collections if r.get("box") == box_id],
            "moves": [r for r in self.moves.values() if r.get("box") == box_id],
        }

    def get_collection(self, point: Any, event: Any) -> dict[str, Any]:
        key = collection_ref_key(point, event)
        if key not in self._collection_index:
            raise NotFoundError(f"unknown collection event: {point}/{event}")
        return self._collection_index[key]

    def find_ref(self, ref: Any) -> tuple[Any, Any, dict[str, Any] | None]:
        point, event = normalize_ref(ref)
        return point, event, self._collection_index.get(collection_ref_key(point, event))

    def collection_view(self, record: dict[str, Any]) -> dict[str, Any]:
        point = record["point"]
        event = record["event"]
        kind = record["kind"]
        occurred = parse_dt(record["occurred_at"])
        effective = occurred
        prior_ref = record.get("prior") or record.get("original_receipt") or record.get("machine_receipt")
        prior_record = None
        prior_valid = False
        prior_errors: list[str] = []

        if kind == "box_check":
            if not prior_ref:
                prior_errors.append("prior_receipt_missing")
            else:
                _, _, prior_record = self.find_ref(prior_ref)
                if prior_record is None:
                    prior_errors.append("prior_receipt_not_found")
                elif prior_record.get("kind") != "machine_in":
                    prior_errors.append("prior_receipt_not_machine_in")
                else:
                    mismatches = []
                    if prior_record.get("box") != record.get("box"):
                        mismatches.append("box")
                    if prior_record.get("issue") != record.get("issue"):
                        mismatches.append("issue")
                    if mismatches:
                        prior_errors.append("prior_receipt_" + "_".join(mismatches) + "_mismatch")
                    else:
                        prior_valid = True
                        effective = parse_dt(prior_record["occurred_at"])

        source = prior_record if prior_valid and prior_record is not None else record
        try:
            sequence = self.collections.index(record)
        except ValueError:
            sequence = None
        return_point = source.get("point", point)
        verify_merchant = self.points.get(point, {}).get("merchant")
        return_merchant = self.points.get(return_point, {}).get("merchant")
        issue_id = record.get("issue")
        issue = self.issues.get(issue_id) if issue_id is not None else None
        issue_box_matches = bool(issue and issue.get("box") == record.get("box"))
        time_ok = bool(issue and effective >= parse_dt(issue["issued_at"]))
        matches_lease = kind in {"confirmed", "box_check"} and issue_box_matches and time_ok

        return {
            **record,
            "sequence": sequence,
            "key": [point, event],
            "ref": f"{point}/{event}",
            "actual_verification": kind in {"confirmed", "box_check"},
            "temporary_receipt": kind == "machine_in",
            "effective_at": iso(effective),
            "effective_at_dt": effective,
            "prior_ref": list(normalize_ref(prior_ref)) if prior_ref else None,
            "prior_valid": prior_valid,
            "prior_errors": prior_errors,
            "return_point": return_point,
            "return_merchant": return_merchant,
            "verification_point": point,
            "verification_merchant": verify_merchant,
            "issue_known": issue is not None,
            "issue_box_matches": issue_box_matches,
            "time_matches_issue": time_ok,
            "matches_lease": matches_lease,
        }

    def collection_views(self, box_id: str | None = None) -> list[dict[str, Any]]:
        records = self.collections if box_id is None else [r for r in self.collections if r.get("box") == box_id]
        views = [self.collection_view(r) for r in records]
        views.sort(key=lambda v: (v["effective_at_dt"], str(v["point"]), str(v["event"])))
        return views

    # ------------------------------------------------------------------
    # Disputes
    # ------------------------------------------------------------------
    def _claim_threads(self, box_id: str) -> list[dict[str, Any]]:
        views = self.collection_views(box_id)
        referenced_machine_keys = set()
        for view in views:
            if view["kind"] == "box_check" and view.get("prior_ref"):
                try:
                    p, e, rec = self.find_ref(view["prior_ref"])
                    if rec is not None and rec.get("kind") == "machine_in":
                        referenced_machine_keys.add(collection_ref_key(p, e))
                except ValidationError:
                    pass

        claims: list[dict[str, Any]] = []
        for view in views:
            if view["actual_verification"]:
                thread_keys = [collection_ref_key(*view["key"])]
                if view.get("prior_ref") and view.get("prior_valid"):
                    thread_keys.append(collection_ref_key(*view["prior_ref"]))
                claim = {k: v for k, v in view.items() if k != "effective_at_dt"}
                claim["effective_at_dt"] = view["effective_at_dt"]
                claim["claim_key"] = view["key"]
                claim["thread_keys"] = thread_keys
                claims.append(claim)
            elif view["kind"] == "machine_in":
                key = collection_ref_key(*view["key"])
                if key not in referenced_machine_keys:
                    claim = {k: v for k, v in view.items() if k != "effective_at_dt"}
                    claim["effective_at_dt"] = view["effective_at_dt"]
                    claim["claim_key"] = view["key"]
                    claim["thread_keys"] = [key]
                    claim["provisional"] = True
                    claims.append(claim)
        claims.sort(
            key=lambda c: (
                c.get("sequence") is None,
                c.get("sequence") if c.get("sequence") is not None else 0,
                c["effective_at_dt"],
            )
        )
        return claims

    def _sequential_clean_rental(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        # Callers may preserve source-report order; chronology is normalized here.
        early, late = sorted((a, b), key=lambda c: c["effective_at_dt"])
        early_issue = self.issues.get(early.get("issue"))
        late_issue = self.issues.get(late.get("issue"))
        if not early_issue or not late_issue or early_issue == late_issue:
            return False
        if early_issue.get("box") != early.get("box") or late_issue.get("box") != late.get("box"):
            return False
        early_at = early["effective_at_dt"]
        late_at = late["effective_at_dt"]
        if late_issue["issue"] == early_issue["issue"]:
            return False
        issued_late = parse_dt(late_issue["issued_at"])
        if issued_late <= early_at or late_at <= early_at:
            return False
        # A box may be assigned to the next lease only after wash acceptance.
        for move_id in self.move_order:
            move = self.moves[move_id]
            if move.get("box") != early.get("box") or move.get("kind") != "wash_ok":
                continue
            moved_at = parse_dt(move["occurred_at"])
            if early_at <= moved_at <= late_at and moved_at <= issued_late:
                return True
        return False

    def list_disputes(self, include_resolved: bool = True) -> list[dict[str, Any]]:
        disputes: list[dict[str, Any]] = []
        for box_id in sorted({r.get("box") for r in self.collections if r.get("box")}):
            claims = self._claim_threads(box_id)
            n = len(claims)
            adjacency: dict[int, set[int]] = defaultdict(set)
            for i in range(n):
                for j in range(i + 1, n):
                    a, b = claims[i], claims[j]
                    if set(a["thread_keys"]) & set(b["thread_keys"]):
                        continue
                    same_issue = a.get("issue") == b.get("issue") and a.get("issue") is not None
                    conflict = same_issue or not self._sequential_clean_rental(a, b)
                    if conflict:
                        adjacency[i].add(j)
                        adjacency[j].add(i)

            seen: set[int] = set()
            for start in range(n):
                if start in seen or not adjacency[start]:
                    continue
                stack = [start]
                component: set[int] = set()
                while stack:
                    idx = stack.pop()
                    if idx in component:
                        continue
                    component.add(idx)
                    seen.add(idx)
                    stack.extend(adjacency[idx] - component)
                if len(component) < 2:
                    continue
                comp_claims = [claims[i] for i in sorted(component)]
                issue_ids = sorted({c.get("issue") for c in comp_claims if c.get("issue")})
                primary_issue = issue_ids[0] if len(issue_ids) == 1 else None
                if primary_issue:
                    dispute_id = f"disp-{primary_issue}"
                else:
                    raw = box_id + ":" + "|".join(c["ref"] for c in comp_claims)
                    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
                    dispute_id = f"disp-{box_id}-{digest}"

                merchants = sorted(
                    {
                        m
                        for c in comp_claims
                        for m in (c.get("return_merchant"), c.get("verification_merchant"))
                        if m
                    }
                )
                points = sorted(
                    {
                        p
                        for c in comp_claims
                        for p in (c.get("point"), c.get("return_point"), c.get("verification_point"))
                        if p
                    }
                )
                customers = sorted(
                    {
                        self.issues[i]["customer"]
                        for i in issue_ids
                        if i in self.issues and self.issues[i].get("customer")
                    }
                )
                has_actual = any(c.get("actual_verification") for c in comp_claims)
                dispute = {
                    "id": dispute_id,
                    "type": "duplicate_possession" if has_actual else "temporary_duplicate",
                    "box": box_id,
                    "issue": primary_issue,
                    "related_issues": issue_ids,
                    "status": "open",
                    "claims": [self._public_claim(c) for c in comp_claims],
                    "merchants": merchants,
                    "points": points,
                    "customers": customers,
                    "visible_to": {"merchants": merchants, "points": points, "customers": customers, "admins": True},
                    "message": "两处以上报告持有同一实物；顾客、相关门店可查，等待管理员核实。",
                }
                resolution = self.dispute_resolutions.get(dispute_id)
                if resolution:
                    dispute["status"] = resolution.get("status", "resolved")
                    dispute["resolution"] = resolution
                    upheld_key = resolution.get("claim_key")
                    for claim in dispute["claims"]:
                        claim["upheld"] = bool(upheld_key and claim["key"] == upheld_key)
                        claim["rejected"] = resolution.get("decision") == "uphold" and not claim["upheld"]
                if dispute["status"] == "open" or include_resolved:
                    disputes.append(dispute)
        disputes.sort(key=lambda d: (d["status"] != "open", d["id"]))
        return disputes

    @staticmethod
    def _public_claim(claim: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in claim.items() if not k.endswith("_dt")}

    def get_dispute(self, dispute_id: str) -> dict[str, Any]:
        for dispute in self.list_disputes():
            if dispute["id"] == dispute_id:
                return dispute
        raise NotFoundError(f"unknown dispute: {dispute_id}")

    def list_conflicts(self, include_resolved: bool = True) -> list[dict[str, Any]]:
        return self.list_disputes(include_resolved)

    def get_conflict(self, conflict_id: str) -> dict[str, Any]:
        return self.get_dispute(conflict_id)

    def resolve_dispute(self, dispute_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        dispute = self.get_dispute(dispute_id)
        decision = payload.get("decision", "uphold")
        if decision not in {"uphold", "reject_all"}:
            raise ValidationError("decision must be 'uphold' or 'reject_all'")

        existing = self.dispute_resolutions.get(dispute_id)
        if existing:
            same = existing.get("decision") == decision
            if decision == "uphold":
                wanted_ref = payload.get("claim") or payload.get("claim_key") or payload.get("upheld_claim")
                if wanted_ref:
                    wanted_key = self._resolve_claim_ref(dispute, wanted_ref)
                    same = same and existing.get("claim_key") == wanted_key
            if not same:
                raise ConflictError(f"dispute {dispute_id} already resolved differently")
            return self.get_dispute(dispute_id)

        resolution: dict[str, Any] = {
            "dispute_id": dispute_id,
            "decision": decision,
            "resolved_by": payload.get("resolved_by") or payload.get("admin") or "admin",
            "resolved_at": payload.get("resolved_at") or iso(datetime.now().astimezone()),
            "note": payload.get("note", ""),
            "correction_ids": [],
        }
        if decision == "uphold":
            wanted_ref = payload.get("claim") or payload.get("claim_key") or payload.get("upheld_claim")
            if not wanted_ref:
                raise ValidationError("uphold resolution requires claim/claim_key")
            claim = self._find_dispute_claim(dispute, wanted_ref)
            resolution["claim_key"] = claim["key"]
            resolution["claim_ref"] = claim["ref"]
            resolution["return_merchant"] = claim.get("return_merchant")

        self.dispute_resolutions[dispute_id] = resolution
        corrections = self._corrections_for_resolution(dispute_id, payload.get("at"))
        resolution["correction_ids"] = [c["id"] for c in corrections]
        return self.get_dispute(dispute_id)

    def _resolve_claim_ref(self, dispute: Mapping[str, Any], ref: Any) -> list[Any] | None:
        claim = self._find_dispute_claim(dispute, ref)
        return claim["key"] if claim else None

    @staticmethod
    def _find_dispute_claim(dispute: Mapping[str, Any], ref: Any) -> dict[str, Any]:
        p, e = normalize_ref(ref)
        for claim in dispute["claims"]:
            if claim["key"] == [p, e]:
                return dict(claim)
            if claim.get("prior_ref") and claim["prior_ref"] == [p, e]:
                return dict(claim)
        raise NotFoundError(f"claim {p}/{e} is not part of dispute {dispute['id']}")

    # ------------------------------------------------------------------
    # Lease / refund eligibility
    # ------------------------------------------------------------------
    def _dispute_for_issue(self, issue_id: str) -> dict[str, Any] | None:
        for dispute in self.list_disputes():
            if issue_id in (dispute.get("related_issues") or []):
                return dispute
        return None

    def issue_view(self, issue_id: str) -> dict[str, Any]:
        issue = self.get_issue(issue_id)
        policy = self.policies.get(issue.get("policy"))
        issued_at = parse_dt(issue["issued_at"])
        deadline = issued_at + SEVEN_DAYS

        collections = [
            self.collection_view(r)
            for r in self.collections
            if r.get("issue") == issue_id or (r.get("box") == issue.get("box") and r.get("issue") == issue_id)
        ]
        collections.sort(key=lambda c: c["effective_at_dt"])
        for cv in collections:
            cv["eligible_verification"] = bool(cv["actual_verification"] and cv["matches_lease"])

        dispute = self._dispute_for_issue(issue_id)
        resolution = self.dispute_resolutions.get(dispute["id"]) if dispute else None
        rejected_keys: set[str] = set()
        allowed_key: list[Any] | None = None
        if resolution and resolution.get("decision") == "uphold":
            allowed_key = resolution.get("claim_key")
            for cv in collections:
                if cv["actual_verification"] and cv["key"] != allowed_key:
                    rejected_keys.add(collection_ref_key(*cv["key"]))
        elif resolution and resolution.get("decision") == "reject_all":
            for cv in collections:
                if cv["actual_verification"]:
                    rejected_keys.add(collection_ref_key(*cv["key"]))

        candidates = [
            cv
            for cv in collections
            if cv["eligible_verification"]
            and collection_ref_key(*cv["key"]) not in rejected_keys
            and not (resolution and resolution.get("decision") == "uphold" and allowed_key and cv["key"] != allowed_key)
        ]
        reported_basis = min(
            candidates,
            key=lambda c: (
                c.get("sequence") is None,
                c.get("sequence") if c.get("sequence") is not None else 0,
                c["effective_at_dt"],
            ),
        ) if candidates else None
        dispute_open = bool(dispute and dispute["status"] == "open")
        # While a possession dispute is open, the earliest matched claim is
        # useful for customer visibility/potential amounts, but it is not an
        # authoritative refund/settlement basis until an administrator rules.
        basis = reported_basis
        authoritative_basis = None if dispute_open else reported_basis

        eligibility_status = "not_returned"
        refund_allowed = False
        if basis:
            on_time = basis["effective_at_dt"] <= deadline
            if dispute and dispute["status"] == "open":
                eligibility_status = "disputed_hold"
            else:
                eligibility_status = "eligible_full" if on_time else "eligible_late_deduction"
                refund_allowed = True
        elif any(c["temporary_receipt"] for c in collections):
            eligibility_status = "awaiting_box_check"
        elif collections:
            eligibility_status = "record_mismatch_or_rejected"

        deduction = 0
        policy_fee = None
        policy_error = None
        if policy is None:
            policy_error = f"unknown policy {issue.get('policy')!r}"
        else:
            policy_fee = policy.get("collection_fee")
            if basis and not (basis["effective_at_dt"] <= deadline):
                deduction = int(policy.get("deduction", 0))

        deposit = int(issue["deposit"])
        gross_due = deposit - deduction if basis else deposit
        base_refund_due = max(0, gross_due) if basis else 0
        base_recovery_due = max(0, -gross_due) if basis else 0
        entries = [dict(e) for e in self.issue_entries.get(issue_id, [])]
        supplementary_refund = sum(
            int(e.get("amount", 0))
            for e in entries
            if e.get("kind") in {"supplementary_refund", "additional_refund", "deduction_reversal"}
            and e.get("state", "confirmed") != "rejected"
        )
        recovery_claimed = base_recovery_due + sum(
            int(e.get("amount", 0))
            for e in entries
            if e.get("kind") == "recovery" and e.get("state", "confirmed") != "rejected"
        )
        objections = [e for e in entries if e.get("kind") == "objection" and e.get("state", "open") == "open"]
        refund_due = base_refund_due
        total_refund_due = base_refund_due + supplementary_refund
        recovery_due = recovery_claimed
        timing = None
        if basis:
            delta = basis["effective_at_dt"] - issued_at
            timing = {
                "issued_at": iso(issued_at),
                "deadline_at": iso(deadline),
                "returned_at": basis["effective_at"],
                "exact_deadline": basis["effective_at_dt"] == deadline,
                "on_time": basis["effective_at_dt"] <= deadline,
                "elapsed_seconds": int(delta.total_seconds()),
                "late_seconds": max(0, int((basis["effective_at_dt"] - deadline).total_seconds())),
                "seven_days": "7*24h",
                "authoritative": not dispute_open,
            }

        refund_info = self._refund_info(issue_id, total_refund_due)
        already_settled = int(refund_info["settled_amount"])
        new_refund_allowed = (
            refund_allowed
            and not policy_error
            and already_settled < total_refund_due
            and not refund_info.get("has_open_channel", False)
        )
        if refund_info["status"] == "settled" and already_settled >= total_refund_due and total_refund_due > 0:
            new_refund_allowed = False

        fee_line = None
        if basis and policy is not None:
            fee_line = {
                "amount": int(policy["collection_fee"]),
                "currency": CURRENCY,
                "payer": issue["merchant"],
                "payee": basis.get("return_merchant"),
                "return_point": basis.get("return_point"),
                "claim_key": basis["key"],
                "dispute_held": bool(dispute and dispute["status"] == "open"),
            }

        try:
            box = self.box_view(issue["box"])
            box_status = box["status"]
        except NotFoundError:
            box_status = None

        return {
            "issue": issue_id,
            "issue_id": issue_id,
            "record": issue,
            "order_id": issue.get("order_id") or issue.get("order") or issue.get("order_ref"),
            "box": issue["box"],
            "customer": issue["customer"],
            "merchant": issue["merchant"],
            "deposit": deposit,
            "deposit_amount": deposit,
            "currency": CURRENCY,
            "refundable_amount": total_refund_due,
            "policy": issue["policy"],
            "policy_error": policy_error,
            "issued_at": issue["issued_at"],
            "deadline_at": iso(deadline),
            "timing": timing,
            "return_basis": self._public_claim(basis) if basis else None,
            "reported_return_basis": self._public_claim(reported_basis) if reported_basis else None,
            "authoritative_return_basis": self._public_claim(authoritative_basis) if authoritative_basis else None,
            "collections": [self._public_claim(c) for c in collections],
            "dispute_id": dispute["id"] if dispute else None,
            "dispute_status": dispute["status"] if dispute else None,
            "eligibility": {
                "status": eligibility_status,
                "refund_allowed": refund_allowed,
                "new_refund_allowed": new_refund_allowed,
                "reason": self._eligibility_reason(eligibility_status),
            },
            "amounts": {
                "deposit": deposit,
                "deduction": deduction,
                "refund_due": total_refund_due,
                "base_refund_due": base_refund_due,
                "supplementary_refund_due": supplementary_refund,
                "total_refund_due": total_refund_due,
                "recovery_due": recovery_due,
                "full_refund": bool(basis and deduction == 0 and supplementary_refund == 0),
            },
            "refund": refund_info,
            "refund_status": refund_info["status"],
            "refund_status_zh": refund_info["status_zh"],
            "settled_amount": refund_info["settled_amount"],
            "issue_entries": entries,
            "objections": objections,
            "service_fee": fee_line,
            "accounts": {
                "deposit_received": deposit,
                "refund_due": total_refund_due,
                "base_refund_due": base_refund_due,
                "supplementary_refund_due": supplementary_refund,
                "recovery_due": recovery_due,
                "refund_settled": already_settled,
                "deduction_retained": deduction if basis else 0,
                "collection_fee": fee_line,
                "fund_scope": f"issue:{issue_id}",
                "segregated": True,
                "new_lease_deposit_protected": True,
            },
            "box_status": box_status,
        }

    @staticmethod
    def _eligibility_reason(status: str) -> str:
        return {
            "not_returned": "没有与租次和实物匹配的点验归还",
            "awaiting_box_check": "仅有归还机临时收件凭证，尚待收箱点开箱点验",
            "record_mismatch_or_rejected": "归还记录与租次/实物不匹配，或点验结论已被管理员否决",
            "disputed_hold": "同一实物存在两处持有报告，退款/费用挂起等待管理员核实",
            "eligible_full": "七天截止时刻前（含截止时刻）实际交回，全额可退",
            "eligible_late_deduction": "超过七天，按领用时政策扣减",
        }.get(status, status)

    def _refund_info(self, issue_id: str, refund_due: int) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self.refunds:
            if record.get("issue") == issue_id:
                groups[record["channel_ref"]].append(record)

        channels = []
        settled_amount = 0
        accepted_amount = 0
        has_open_channel = False
        for ref, rows in groups.items():
            rows = sorted(rows, key=lambda r: parse_dt(r["occurred_at"]))
            settled_rows = [r for r in rows if r.get("state") == "settled"]
            accepted_rows = [r for r in rows if r.get("state") == "accepted"]
            channel_settled = sum(int(r.get("amount", 0)) for r in settled_rows)
            latest = rows[-1]
            state = "settled" if settled_rows else latest.get("state")
            if accepted_rows and not settled_rows:
                state = "accepted"
            if state in {"submitted", "accepted"}:
                has_open_channel = True
            settled_amount += channel_settled
            accepted_amount += 0 if settled_rows else int(rows[-1].get("amount", 0))
            channels.append(
                {
                    "channel_ref": ref,
                    "state": state,
                    "amount": rows[-1].get("amount"),
                    "settled_amount": channel_settled,
                    "events": rows,
                }
            )

        if settled_amount > 0:
            if refund_due and settled_amount < refund_due:
                status = "partially_settled"
            else:
                status = "settled"
        elif accepted_amount > 0 or any(c["state"] == "accepted" for c in channels):
            status = "accepted"
        elif channels:
            status = channels[-1]["state"] or "submitted"
        else:
            status = "not_requested"

        zh = {
            "not_requested": "未申请退款",
            "submitted": "退款申请已提交，待收付方受理",
            "accepted": "收付方已受理，待到账确认",
            "settled": "已到账",
            "partially_settled": "部分退款已到账",
            "failed": "退款失败",
            "rejected": "收付方退回退款申请",
        }.get(status, status)
        return {
            "status": status,
            "status_zh": zh,
            "completed": status == "settled",
            "settled_amount": settled_amount,
            "accepted_pending_amount": accepted_amount,
            "refund_due": refund_due,
            "channels": channels,
            "confirmation_rule": "only state=settled is completed; accepted is only accepted by payment party",
            "has_open_channel": has_open_channel,
        }

    # ------------------------------------------------------------------
    # Refund commands
    # ------------------------------------------------------------------
    def create_refund(self, issue_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        view = self.issue_view(issue_id)
        eligibility = view["eligibility"]
        if not view["return_basis"]:
            raise BusinessRuleError(eligibility["reason"])
        if view["dispute_status"] == "open":
            raise BusinessRuleError("refund is held by an unresolved possession dispute")
        if view["policy_error"]:
            raise BusinessRuleError(view["policy_error"])
        if view["refund"]["has_open_channel"]:
            raise ConflictError("an existing refund channel is still pending")

        due = int(view["amounts"]["refund_due"])
        already = int(view["refund"]["settled_amount"])
        remaining = max(0, due - already)
        amount = int(payload.get("amount", remaining))
        if amount <= 0:
            raise ValidationError("refund amount must be positive")
        if amount > remaining and not payload.get("allow_over_refund"):
            raise BusinessRuleError(f"refund amount {amount} exceeds remaining refund due {remaining}")

        self.counters["refund"] += 1
        channel_ref = payload.get("channel_ref") or payload.get("idempotency_key") or (
            f"ref-{issue_id}-{self.counters['refund']:03d}"
        )
        if any(r.get("channel_ref") == channel_ref for r in self.refunds):
            raise ConflictError(f"channel_ref already exists: {channel_ref}")
        record = {
            "channel_ref": channel_ref,
            "issue": issue_id,
            "amount": amount,
            "state": payload.get("initial_state", "submitted"),
            "occurred_at": payload.get("occurred_at") or iso(datetime.now().astimezone()),
        }
        if record["state"] not in {"submitted", "accepted", "settled", "failed", "rejected"}:
            raise ValidationError("invalid initial refund state")
        self.ingest(record, "refunds")
        return self.issue_view(issue_id)

    def transition_refund(
        self, channel_ref: str, state: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        rows = [r for r in self.refunds if r.get("channel_ref") == channel_ref]
        if not rows:
            raise NotFoundError(f"unknown refund channel_ref: {channel_ref}")
        issue_id = rows[0]["issue"]
        amount = payload.get("amount", rows[-1].get("amount"))
        record = {
            "channel_ref": channel_ref,
            "issue": issue_id,
            "amount": int(amount),
            "state": state,
            "occurred_at": payload.get("occurred_at") or iso(datetime.now().astimezone()),
        }
        self.ingest(record, "refunds")
        return self._refund_info(issue_id, self.issue_view(issue_id)["amounts"]["refund_due"])

    def add_issue_entry(self, issue_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record an old-lease objection, supplementary refund, or recovery.

        Entries are deliberately keyed by ``issue_id``.  The service never
        deducts them from another issue's deposit.
        """
        self.get_issue(issue_id)
        kind = payload.get("kind")
        allowed = {
            "supplementary_refund",
            "additional_refund",
            "deduction_reversal",
            "recovery",
            "objection",
            "note",
        }
        if kind not in allowed:
            raise ValidationError(f"kind must be one of {sorted(allowed)}")
        amount = int(payload.get("amount", 0) or 0)
        if kind in {"supplementary_refund", "additional_refund", "deduction_reversal", "recovery"} and amount <= 0:
            raise ValidationError(f"{kind} requires a positive integer amount")
        self.counters["issue_entry"] += 1
        entry_id = payload.get("entry_id") or f"IE-{issue_id}-{self.counters['issue_entry']:03d}"
        entries = self.issue_entries.setdefault(issue_id, [])
        if any(e.get("id") == entry_id for e in entries):
            raise ConflictError(f"issue entry already exists: {entry_id}")
        entry = {
            "id": entry_id,
            "issue": issue_id,
            "kind": kind,
            "amount": amount,
            "reason": payload.get("reason", ""),
            "actor": payload.get("actor") or payload.get("by") or "admin",
            "state": payload.get("state", "open" if kind == "objection" else "confirmed"),
            "occurred_at": payload.get("at") or payload.get("occurred_at") or iso(datetime.now().astimezone()),
            "lease_scope": f"issue:{issue_id}",
            "uses_only_same_lease_funds": True,
            "new_lease_deposit_protected": True,
        }
        entries.append(entry)
        return self.issue_view(issue_id)

    # ------------------------------------------------------------------
    # Rentals, orders, moves and box tracking
    # ------------------------------------------------------------------
    def create_issue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _require(payload, ["issue", "box", "merchant", "customer", "policy", "deposit"], "issue command")
        issue_id = str(payload["issue"])
        if issue_id in self.issues:
            raise ConflictError(f"issue {issue_id} already exists")
        if payload["policy"] not in self.policies:
            raise ValidationError(f"unknown policy: {payload['policy']}")
        box_id = str(payload["box"])
        related = self.get_box_records(box_id)
        if any(related.values()) and not payload.get("allow_unavailable"):
            box = self.box_view(box_id)
            if not box["rentable"] and not payload.get("force"):
                raise BusinessRuleError(
                    f"box {box_id} is not rentable: {box['status']} / {box.get('rentability_reason')}"
                )
        record = dict(payload)
        record["issued_at"] = payload.get("issued_at") or iso(datetime.now().astimezone())
        self.ingest(record, "issues")
        return self.issue_view(issue_id)

    def create_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _require(payload, ["order_id", "customer", "merchant", "policy", "boxes"], "order")
        boxes = payload["boxes"]
        if not isinstance(boxes, list) or not boxes:
            raise ValidationError("boxes must be a non-empty list")
        deposit_each = payload.get("deposit_per_box", payload.get("deposit"))
        deposits = payload.get("deposits", {})
        planned = []
        for idx, box_id in enumerate(boxes, 1):
            issue_id = payload.get("issue_ids", {}).get(box_id) if isinstance(payload.get("issue_ids"), Mapping) else None
            issue_id = issue_id or f"{payload['order_id']}-{idx:02d}"
            deposit = deposits.get(box_id, deposit_each)
            if deposit is None:
                raise ValidationError(f"deposit missing for box {box_id}")
            candidate = {
                "issue": issue_id,
                "box": box_id,
                "order_id": payload["order_id"],
                "merchant": payload["merchant"],
                "customer": payload["customer"],
                "policy": payload["policy"],
                "deposit": deposit,
                "issued_at": payload.get("issued_at"),
            }
            # Preflight without mutating so one unavailable box rejects the order.
            if issue_id in self.issues:
                raise ConflictError(f"issue {issue_id} already exists")
            if payload["policy"] not in self.policies:
                raise ValidationError(f"unknown policy: {payload['policy']}")
            related = self.get_box_records(box_id)
            if any(related.values()):
                box = self.box_view(box_id)
                if not box["rentable"] and not payload.get("force"):
                    raise BusinessRuleError(f"box {box_id} is not rentable: {box['status']}")
            planned.append(candidate)
        views = [self.create_issue({k: v for k, v in item.items() if v is not None or k != "issued_at"}) for item in planned]
        return {"order_id": payload["order_id"], "customer": payload["customer"], "issues": views}

    def record_move(self, payload: Mapping[str, Any], *, strict: bool | None = None) -> dict[str, Any]:
        data = dict(payload)
        if strict is None:
            strict = bool(data.pop("strict_custody", data.pop("strict", True)))
        else:
            data.pop("strict_custody", None)
            data.pop("strict", None)
        if strict and data.get("kind") == "handoff":
            try:
                current = self.box_view(str(data["box"]))
                expected = current.get("custodian")
                if (
                    expected
                    and data.get("from")
                    and data["from"] != expected
                    and not str(expected).startswith("customer:")
                ):
                    raise BusinessRuleError(
                        f"handoff from={data['from']} does not match current custodian {expected}"
                    )
            except NotFoundError:
                pass
        self.ingest(data, "moves")
        return self.box_view(data["box"])

    def box_view(self, box_id: str) -> dict[str, Any]:
        related = self.get_box_records(box_id)
        if not any(related.values()):
            raise NotFoundError(f"unknown box: {box_id}")

        events: list[dict[str, Any]] = []
        for rec in related["issues"]:
            events.append(
                {
                    "at": parse_dt(rec["issued_at"]),
                    "type": "issue",
                    "ref": rec["issue"],
                    "detail": f"租次 {rec['issue']} 发给顾客 {rec['customer']}",
                    "raw": rec,
                }
            )
        for rec in related["collections"]:
            cv = self.collection_view(rec)
            events.append(
                {
                    "at": parse_dt(rec["occurred_at"]),
                    "effective_at": cv["effective_at_dt"],
                    "type": rec["kind"],
                    "ref": cv["ref"],
                    "detail": self._collection_event_detail(cv),
                    "view": cv,
                    "raw": rec,
                }
            )
        for rec in related["moves"]:
            events.append(
                {
                    "at": parse_dt(rec["occurred_at"]),
                    "type": "move:" + rec["kind"],
                    "ref": rec["move"],
                    "detail": f"{rec.get('from', '?')} → {rec.get('to', '?')} ({rec['kind']})",
                    "raw": rec,
                }
            )
        events.sort(key=lambda e: (e["at"], e["type"], e["ref"]))

        status = "available"
        custodian: str | None = None
        location: str | None = None
        clean_certified = False
        active_issue: str | None = None
        warnings: list[str] = []
        last_handoff_to_cleaner: dict[str, Any] | None = None

        # Resolutions tell us which physical claim is authoritative.
        rejected_collection_keys: set[str] = set()
        for dispute in self.list_disputes():
            if dispute.get("box") != box_id:
                continue
            res = self.dispute_resolutions.get(dispute["id"])
            if res and res.get("decision") == "uphold":
                upheld = res.get("claim_key")
                for claim in dispute["claims"]:
                    if claim["key"] != upheld:
                        rejected_collection_keys.add(collection_ref_key(*claim["key"]))
            elif res and res.get("decision") == "reject_all":
                for claim in dispute["claims"]:
                    rejected_collection_keys.add(collection_ref_key(*claim["key"]))

        for event in events:
            if event["type"] == "issue":
                rec = event["raw"]
                status = "rented"
                active_issue = rec["issue"]
                custodian = f"customer:{rec['customer']}"
                location = None
            elif event["type"] == "machine_in":
                cv = event["view"]
                location = cv["point"]
                if status in {"rented", "with_customer", "available", "pending_cleaning"}:
                    status = "at_machine_pending_check"
            elif event["type"] in {"confirmed", "box_check"}:
                cv = event["view"]
                if collection_ref_key(*cv["key"]) in rejected_collection_keys:
                    continue
                if event["type"] == "box_check" and not cv.get("prior_valid"):
                    warnings.append(f"{cv['ref']}: 开箱点验缺少有效原收件凭证，计时按点验时刻")
                location = cv.get("verification_point")
                custodian = cv.get("verification_merchant") or cv.get("return_merchant")
                status = "pending_cleaning"
                if active_issue and cv.get("issue") == active_issue and cv.get("matches_lease"):
                    active_issue = None
                clean_certified = False
            elif event["type"].startswith("move:"):
                rec = event["raw"]
                kind = rec["kind"]
                expected_from = custodian
                if kind == "handoff":
                    if expected_from and rec.get("from") and rec["from"] != expected_from and not str(expected_from).startswith("customer:"):
                        warnings.append(
                            f"move {rec['move']}: from={rec.get('from')} 与当前保管人 {expected_from} 不一致"
                        )
                    custodian = rec.get("to")
                    location = rec.get("to")
                    if str(rec.get("to", "")).startswith("wash") or rec.get("purpose") == "cleaning":
                        status = "pending_cleaning"
                        last_handoff_to_cleaner = rec
                        if not rec.get("handoff") and not rec.get("handoff_ref"):
                            # The exchange sample may infer the matching handoff.
                            pass
                    else:
                        status = "in_transit" if rec.get("in_transit") else "pending_cleaning"
                    clean_certified = False
                elif kind == "wash_ok":
                    handoff_ref = rec.get("handoff") or rec.get("handoff_ref") or rec.get("prior")
                    if handoff_ref:
                        _, _, handoff = self.find_ref(handoff_ref) if isinstance(handoff_ref, list) else (None, None, self.moves.get(str(handoff_ref)))
                        if not handoff:
                            warnings.append(f"move {rec['move']}: wash_ok references unknown handoff {handoff_ref}")
                    elif last_handoff_to_cleaner is None:
                        warnings.append(f"move {rec['move']}: wash_ok 未引用同一次保管移交")
                    custodian = rec.get("to", rec.get("from"))
                    location = custodian
                    status = "available"
                    clean_certified = True
                    if active_issue:
                        warnings.append(
                            f"move {rec['move']}: wash_ok exists but lease {active_issue} lacks a matched point check"
                        )
                elif kind == "wash_fail":
                    handoff_ref = rec.get("handoff") or rec.get("handoff_ref") or rec.get("prior")
                    if handoff_ref:
                        _, _, handoff = self.find_ref(handoff_ref) if isinstance(handoff_ref, list) else (None, None, self.moves.get(str(handoff_ref)))
                        if not handoff:
                            warnings.append(f"move {rec['move']}: wash_fail references unknown handoff {handoff_ref}")
                    custodian = rec.get("to", rec.get("from"))
                    location = custodian
                    status = "wash_failed"
                    clean_certified = False
                    if active_issue:
                        warnings.append(
                            f"move {rec['move']}: wash_fail exists but lease {active_issue} lacks a matched point check"
                        )
                elif kind in {"ship", "dispatch", "in_transit"}:
                    custodian = rec.get("to")
                    location = rec.get("to")
                    status = "in_transit"
                elif kind in {"receive", "arrive"}:
                    custodian = rec.get("to")
                    location = rec.get("to")
                    status = "available" if rec.get("wash_ok") else "pending_cleaning"
                else:
                    # Generic transfer: preserve custody even for unknown kinds.
                    if rec.get("to"):
                        custodian = rec["to"]
                        location = rec["to"]

        # A clean acceptance makes the physical box available; an unresolved
        # matching lease is retained as a data/custody warning and blocks issue.
        rentable = status == "available" and clean_certified and active_issue is None
        reason = None
        if not rentable:
            if active_issue and status == "available":
                reason = "matched wash acceptance exists but the prior lease lacks a closed point check"
            else:
                reason = {
                    "rented": "active lease",
                    "at_machine_pending_check": "machine receipt not checked",
                    "pending_cleaning": "not cleaning-accepted yet",
                    "in_transit": "box is in transit",
                    "wash_failed": "wash failed; repair/disposal only",
                }.get(status, status)

        dispute = next((d for d in self.list_disputes() if d.get("box") == box_id and d["status"] == "open"), None)
        timeline = []
        for e in events:
            item = {k: v for k, v in e.items() if k not in {"at", "effective_at", "raw", "view"}}
            item["at"] = iso(e["at"])
            if "effective_at" in e:
                item["effective_at"] = iso(e["effective_at"])
            timeline.append(item)

        return {
            "box": box_id,
            "status": status,
            "status_zh": STATUS_ZH.get(status, status),
            "tracking_state": status,
            "custodian": custodian,
            "location": location,
            "clean_certified": clean_certified,
            "rentable": rentable,
            "available": rentable,
            "rentability_reason": reason,
            "active_issue": active_issue,
            "open_dispute_id": dispute["id"] if dispute else None,
            "custody_disputed": bool(dispute),
            "warnings": warnings,
            "timeline": timeline,
            "counts": {
                "issues": len(related["issues"]),
                "collections": len(related["collections"]),
                "moves": len(related["moves"]),
            },
        }

    @staticmethod
    def _collection_event_detail(cv: Mapping[str, Any]) -> str:
        if cv["kind"] == "machine_in":
            return f"归还机 {cv['point']} 临时收件，不能单独作为退款/清洗责任依据"
        if cv["kind"] == "box_check":
            extra = "，已关联原收件凭证" if cv.get("prior_valid") else "，缺少有效原收件凭证"
            return f"收箱点 {cv['point']} 开箱点验{extra}"
        return f"门店 {cv['point']} 当面点验"

    # ------------------------------------------------------------------
    # Customer / merchant / admin views
    # ------------------------------------------------------------------
    def customer_view(self, customer_id: str) -> dict[str, Any]:
        issue_ids = [iid for iid, rec in self.issues.items() if rec.get("customer") == customer_id]
        if not issue_ids and not any(customer_id in d.get("customers", []) for d in self.list_disputes()):
            raise NotFoundError(f"unknown customer or no visible boxes: {customer_id}")
        issues = [self.issue_view(iid) for iid in sorted(issue_ids)]
        disputes = [d for d in self.list_disputes() if customer_id in d.get("customers", [])]
        return {
            "customer": customer_id,
            "issues": issues,
            "boxes": [
                {
                    "issue": v["issue"],
                    "box": v["box"],
                    "order_id": v.get("order_id"),
                    "refund_status": v["refund"]["status"],
                    "refund_status_zh": v["refund"]["status_zh"],
                    "eligibility_status": v["eligibility"]["status"],
                    "deposit": v["deposit"],
                    "refund_due": v["amounts"]["refund_due"],
                    "settled_amount": v["refund"]["settled_amount"],
                    "dispute_id": v["dispute_id"],
                }
                for v in issues
            ],
            "disputes": disputes,
            "single_box_rule": "each issue is independent; returning one box never changes another issue",
        }

    def merchant_view(self, merchant_id: str, period: str | None = None) -> dict[str, Any]:
        actor_id = merchant_id
        actor_point = self.points.get(merchant_id)
        if actor_point and actor_point.get("merchant"):
            merchant_id = actor_point["merchant"]
        owned_points = sorted(pid for pid, rec in self.points.items() if rec.get("merchant") == merchant_id)
        issue_views = [self.issue_view(iid) for iid in sorted(self.issues) if self.issues[iid].get("merchant") == merchant_id]
        collection_views = [
            cv for cv in self.collection_views() if cv.get("return_merchant") == merchant_id or cv.get("verification_merchant") == merchant_id
        ]
        collected = []
        for cv in collection_views:
            issue = self.issues.get(cv.get("issue"))
            item = {
                "ref": cv["ref"],
                "point": cv["point"],
                "return_point": cv["return_point"],
                "effective_at": cv["effective_at"],
                "kind": cv["kind"],
                "issue": cv.get("issue"),
                "box": cv.get("box"),
                "original_merchant": issue.get("merchant") if issue else None,
                "customer": issue.get("customer") if issue else None,
                "for_other_merchant": bool(issue and issue.get("merchant") != merchant_id),
                "matches_lease": cv.get("matches_lease"),
            }
            if issue:
                iv = self.issue_view(issue["issue"])
                policy = self.policies.get(issue.get("policy"))
                potential_fee = None
                if cv.get("actual_verification") and cv.get("matches_lease") and policy:
                    potential_fee = {
                        "amount": int(policy["collection_fee"]),
                        "currency": CURRENCY,
                        "payer": issue.get("merchant"),
                        "payee": cv.get("return_merchant"),
                        "return_point": cv.get("return_point"),
                        "claim_key": cv.get("key"),
                    }
                item["deposit_received_by_original_store"] = iv["deposit"]
                item["refund_due"] = iv["amounts"]["refund_due"]
                item["refund_status"] = iv["refund"]["status"]
                item["service_fee"] = iv["service_fee"]
                item["potential_service_fee"] = potential_fee
                item["dispute_id"] = iv["dispute_id"]
                item["dispute_status"] = iv["dispute_status"]
            collected.append(item)

        def sum_issue_attr(predicate):
            return sum(predicate(v) for v in issue_views)

        fees_payable = 0
        fees_payable_ready = 0
        disputed_payable = 0
        for v in issue_views:
            fee = v.get("service_fee")
            if not fee or fee.get("payee") == merchant_id:
                continue
            if v["dispute_status"] == "open":
                disputed_payable += fee["amount"]
            else:
                fees_payable += fee["amount"]
                fees_payable_ready += fee["amount"]

        fees_receivable = 0
        disputed_receivable = 0
        receivable_records = []
        for line in self.settlement_lines(period):
            if line.get("payee") == merchant_id:
                receivable_records.append(line)
                if line["status"] == "ready":
                    fees_receivable += line["amount"]
        # Every competing verified claim is visible to its own merchant.  While
        # unresolved, each merchant sees its own potential receivable as held.
        for item in collected:
            fee = item.get("potential_service_fee")
            if (
                fee
                and fee.get("payee") == merchant_id
                and item.get("dispute_status") == "open"
                and item.get("for_other_merchant")
            ):
                disputed_receivable += int(fee["amount"])
        fees_disputed = disputed_payable + disputed_receivable

        custody_boxes = []
        for box_id in sorted({r.get("box") for r in self.issues.values()} | {r.get("box") for r in self.moves.values()}):
            try:
                bv = self.box_view(box_id)
            except NotFoundError:
                continue
            if bv.get("custodian") == merchant_id or bv.get("location") in owned_points:
                custody_boxes.append({k: bv[k] for k in ("box", "status", "status_zh", "custodian", "location", "rentable", "warnings")})

        related_settlements = [
            s for s in self.settlements.values() if merchant_id in json.dumps(s.get("balances", []), ensure_ascii=False)
            or any(l.get("payer") == merchant_id or l.get("payee") == merchant_id for l in s.get("lines", []))
        ]
        related_adjustments = [
            a for a in self.adjustments.values() if any(e.get("merchant") == merchant_id for e in a.get("entries", []))
        ]

        return {
            "actor": actor_id,
            "merchant": merchant_id,
            "requested_point": actor_id if actor_point else None,
            "points": owned_points,
            "period": period,
            "deposits_received": {
                "count": len(issue_views),
                "amount": sum_issue_attr(lambda v: v["deposit"]),
                "currency": CURRENCY,
            },
            "refunds": {
                "settled_amount": sum_issue_attr(lambda v: v["refund"]["settled_amount"]),
                "due_amount": sum_issue_attr(lambda v: v["amounts"]["refund_due"]),
                "pending_channels": sum(1 for v in issue_views if v["refund"]["has_open_channel"]),
            },
            "deductions_retained": sum_issue_attr(lambda v: v["accounts"]["deduction_retained"]),
            "collection_fees": {
                "payable_ready": fees_payable_ready,
                "receivable_ready": fees_receivable,
                "disputed_payable": disputed_payable,
                "disputed_receivable": disputed_receivable,
                "disputed_or_held": fees_disputed,
                "currency": CURRENCY,
            },
            "issues": issue_views,
            "collections": collected,
            "collections_on_behalf_of_other_stores": [c for c in collected if c.get("for_other_merchant")],
            "fee_lines": receivable_records,
            "custody_boxes": custody_boxes,
            "settlements": related_settlements,
            "adjustments": related_adjustments,
            "same_business_trace": "issue -> deposit/refund -> valid collection -> service fee -> settlement/adjustment",
        }

    def point_view(self, point_id: str) -> dict[str, Any]:
        point = self.points.get(point_id)
        if point is None:
            # Merchant IDs are also valid actors; expose a useful alias rather
            # than forcing callers to know whether an ID is a point or merchant.
            if any(p.get("merchant") == point_id for p in self.points.values()):
                return {"merchant_alias": True, **self.merchant_view(point_id)}
            raise NotFoundError(f"unknown return point: {point_id}")
        merchant_id = point.get("merchant")
        collections = []
        for cv in self.collection_views():
            if point_id in {cv.get("point"), cv.get("return_point"), cv.get("verification_point")}:
                item = {k: v for k, v in cv.items() if k != "effective_at_dt"}
                if cv.get("issue") in self.issues:
                    item["business"] = self.issue_view(cv["issue"])["accounts"]
                collections.append(item)
        disputes = [
            d
            for d in self.list_disputes()
            if point_id in d.get("points", [])
            or any(point_id in c.get("thread_keys", []) for c in d.get("claims", []))
        ]
        return {
            "point": point_id,
            "merchant": merchant_id,
            "point_record": point,
            "collections": collections,
            "disputes": disputes,
            "merchant_view": self.merchant_view(merchant_id) if merchant_id else None,
            "role": "return_point",
        }

    def admin_overview(self) -> dict[str, Any]:
        box_ids = sorted(
            {r.get("box") for r in self.issues.values()}
            | {r.get("box") for r in self.moves.values()}
            | {r.get("box") for r in self.collections}
        )
        boxes = []
        for box in box_ids:
            try:
                boxes.append(self.box_view(box))
            except NotFoundError:
                pass
        return {
            "counts": {
                "policies": len(self.policies),
                "points": len(self.points),
                "issues": len(self.issues),
                "collections_unique": len(self.collections),
                "moves": len(self.moves),
                "refund_events": len(self.refunds),
                "boxes": len(boxes),
                "open_disputes": sum(1 for d in self.list_disputes() if d["status"] == "open"),
                "settlements": len(self.settlements),
                "adjustments": len(self.adjustments),
            },
            "disputes": self.list_disputes(),
            "boxes": [
                {k: b[k] for k in ("box", "status", "status_zh", "custodian", "rentable", "open_dispute_id", "warnings")}
                for b in boxes
            ],
            "settlements": list(self.settlements.values()),
            "adjustments": list(self.adjustments.values()),
            "integrity": self.integrity_report(),
        }

    def integrity_report(self) -> dict[str, Any]:
        problems: list[dict[str, Any]] = []
        for issue in self.issues.values():
            if issue.get("policy") not in self.policies:
                problems.append({"type": "unknown_policy", "issue": issue["issue"], "policy": issue.get("policy")})
        for point in self.points.values():
            if not point.get("merchant"):
                problems.append({"type": "point_without_merchant", "point": point.get("point")})
        for cv in self.collection_views():
            if not cv["issue_known"]:
                problems.append({"type": "unknown_issue", "ref": cv["ref"], "issue": cv.get("issue")})
            elif not cv["issue_box_matches"]:
                problems.append(
                    {
                        "type": "issue_box_mismatch",
                        "ref": cv["ref"],
                        "issue": cv.get("issue"),
                        "record_box": cv.get("box"),
                        "issue_box": self.issues[cv["issue"]].get("box"),
                    }
                )
            if cv["kind"] == "box_check":
                for err in cv["prior_errors"]:
                    problems.append({"type": err, "ref": cv["ref"]})
            if cv.get("return_point") and cv.get("return_merchant") is None:
                problems.append({"type": "unknown_return_point", "ref": cv["ref"], "point": cv["return_point"]})
        for box_id in sorted({r.get("box") for r in self.moves.values()}):
            bv = self.box_view(box_id)
            for warning in bv.get("warnings", []):
                problems.append({"type": "custody_chain_warning", "box": box_id, "message": warning})
        return {"problem_count": len(problems), "problems": problems}

    # ------------------------------------------------------------------
    # Monthly service-fee settlements and post-settlement corrections
    # ------------------------------------------------------------------
    def settlement_lines(self, period: str | None = None) -> list[dict[str, Any]]:
        lines = []
        for issue_id in sorted(self.issues):
            view = self.issue_view(issue_id)
            basis = view.get("return_basis")
            fee = view.get("service_fee")
            if not basis or not fee:
                continue
            effective = basis["effective_at"]
            line_period = effective[:7]
            if period and line_period != period:
                continue
            status = "ready"
            if view["dispute_status"] == "open" or fee.get("dispute_held"):
                status = "held"
            elif not fee.get("payee"):
                status = "invalid_point"
            lines.append(
                {
                    "line_id": f"fee-{line_period}-{issue_id}",
                    "period": line_period,
                    "issue": issue_id,
                    "box": view["box"],
                    "effective_at": effective,
                    "payer": fee["payer"],
                    "payee": fee["payee"],
                    "amount": fee["amount"],
                    "currency": CURRENCY,
                    "return_point": fee.get("return_point"),
                    "claim_key": fee.get("claim_key"),
                    "status": status,
                    "dispute_id": view.get("dispute_id"),
                    "lease_scoped": True,
                }
            )
        return lines

    def create_settlement(
        self,
        period: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        if not MONTH_RE.match(period or ""):
            raise ValidationError("period must be YYYY-MM")
        settlement_id = payload.get("settlement_id") or f"STL-{period}"
        if settlement_id in self.settlements:
            existing = self.settlements[settlement_id]
            if existing.get("status") == "finalized" and not payload.get("replace_finalized"):
                result = dict(existing)
                result["already_finalized"] = True
                return result
            if existing.get("status") == "draft" and not payload.get("replace", True):
                raise ConflictError(f"settlement {settlement_id} already exists")

        all_lines = self.settlement_lines(period)
        include_held = bool(payload.get("include_held", False))
        lines = []
        held_lines = []
        invalid_lines = []
        for line in all_lines:
            if line["status"] == "ready":
                lines.append(line)
            elif line["status"] == "held":
                held_lines.append(line)
                if include_held:
                    lines.append({**line, "payable": False, "included_as": "held_provisional"})
            else:
                invalid_lines.append(line)

        balances_map: dict[tuple[str, str], int] = defaultdict(int)
        for line in lines:
            if line.get("payable", True) is False:
                continue
            balances_map[(line["payer"], line["payee"])] += line["amount"]
        balances = [
            {"payer": payer, "payee": payee, "amount": amount, "currency": CURRENCY}
            for (payer, payee), amount in sorted(balances_map.items())
        ]
        finalize = bool(payload.get("finalize", True))
        now = payload.get("finalized_at") or payload.get("at") or iso(datetime.now().astimezone())
        settlement = {
            "id": settlement_id,
            "period": period,
            "month": period,
            "status": "finalized" if finalize else "draft",
            "currency": CURRENCY,
            "created_at": payload.get("created_at") or now,
            "finalized_at": now if finalize else None,
            "lines": lines,
            "held_lines": held_lines,
            "invalid_lines": invalid_lines,
            "balances": balances,
            "total_amount": sum(b["amount"] for b in balances),
            "settlement_is_not_box_availability": True,
        }
        self.settlements[settlement_id] = settlement
        if settlement_id not in self.settlement_order:
            self.settlement_order.append(settlement_id)
        return settlement

    def settle_month(self, period: str, at: str | None = None) -> dict[str, Any]:
        return self.create_settlement(period, {"finalize": True, "at": at, "finalized_at": at})

    def list_settlements(self) -> list[dict[str, Any]]:
        return [self.settlements[sid] for sid in self.settlement_order if sid in self.settlements]

    def _corrections_for_resolution(self, dispute_id: str, at: str | None = None) -> list[dict[str, Any]]:
        dispute = self.get_dispute(dispute_id)
        resolution = self.dispute_resolutions[dispute_id]
        issue_ids = dispute.get("related_issues") or []
        corrections = []
        for issue_id in issue_ids:
            view = self.issue_view(issue_id)
            fee = view.get("service_fee")
            correct_payee = fee.get("payee") if fee and view["dispute_status"] == "resolved" else None
            correct_amount = int(fee["amount"]) if fee and correct_payee else 0
            for settlement in self.settlements.values():
                if settlement.get("status") != "finalized":
                    continue
                for line in settlement.get("lines", []):
                    if line.get("issue") != issue_id:
                        continue
                    if line.get("payable") is False:
                        continue
                    already = any(
                        a.get("resolution_id") == dispute_id and a.get("original_line_id") == line.get("line_id")
                        for a in self.adjustments.values()
                    )
                    if already:
                        continue
                    if line.get("payee") == correct_payee and int(line.get("amount", 0)) == correct_amount:
                        continue
                    correction = self._make_correction(
                        settlement,
                        line,
                        correct_payee,
                        correct_amount,
                        at=at,
                        reason=f"admin resolution {dispute_id}: {resolution.get('note') or 'misidentification corrected'}",
                        resolution_id=dispute_id,
                    )
                    corrections.append(correction)
        return corrections

    def _make_correction(
        self,
        settlement: Mapping[str, Any],
        line: Mapping[str, Any],
        correct_payee: str | None,
        correct_amount: int,
        *,
        at: str | None = None,
        reason: str = "",
        resolution_id: str | None = None,
    ) -> dict[str, Any]:
        self.counters["adjustment"] += 1
        adjustment_id = f"ADJ-{self.counters['adjustment']:04d}"
        original_amount = int(line.get("amount", 0))
        payer = line["payer"]
        original_payee = line["payee"]
        delta_amount = correct_amount - original_amount
        entries = [
            {
                "merchant": original_payee,
                "account": "collection_fee_original_settlement_reversal",
                "delta_amount": -original_amount,
                "currency": CURRENCY,
                "reference": settlement["id"],
            },
            {
                "merchant": payer,
                "account": "collection_fee_clearing",
                "delta_amount": original_amount - correct_amount,
                "currency": CURRENCY,
                "reference": adjustment_id,
            },
        ]
        if correct_payee and correct_amount:
            entries.insert(
                1,
                {
                    "merchant": correct_payee,
                    "account": "collection_fee_correct_payee",
                    "delta_amount": correct_amount,
                    "currency": CURRENCY,
                    "reference": adjustment_id,
                },
            )
        adjustment = {
            "id": adjustment_id,
            "kind": "pointed_settlement_difference",
            "type": "misidentification_delta",
            "created_at": at or iso(datetime.now().astimezone()),
            "reason": reason,
            "resolution_id": resolution_id,
            "original_settlement_id": settlement["id"],
            "original_line_id": line.get("line_id"),
            "original_settlement_ref": settlement["id"],
            "points_to_original_settlement": True,
            "period": line.get("period") or settlement.get("period"),
            "issue": line.get("issue"),
            "box": line.get("box"),
            "lease_scope": f"issue:{line.get('issue')}",
            "payer": payer,
            "original_payee": original_payee,
            "correct_payee": correct_payee,
            "original_amount": original_amount,
            "correct_amount": correct_amount,
            "delta_amount": delta_amount,
            "amount_difference": delta_amount,
            "currency": CURRENCY,
            "entries": entries,
            "does_not_debit_next_lease": True,
            "next_lease_collection_point_protected": True,
        }
        self.adjustments[adjustment_id] = adjustment
        self.adjustment_order.append(adjustment_id)
        return adjustment

    def correct_settlement(self, settlement_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if settlement_id not in self.settlements:
            raise NotFoundError(f"unknown settlement: {settlement_id}")
        settlement = self.settlements[settlement_id]
        if settlement.get("status") != "finalized" and not payload.get("allow_draft"):
            raise BusinessRuleError("corrections point to a finalized settlement")
        issue_id = payload.get("issue")
        line = next((l for l in settlement.get("lines", []) if l.get("issue") == issue_id), None)
        if line is None:
            raise NotFoundError(f"settlement {settlement_id} has no line for issue {issue_id}")
        correct_payee = payload.get("correct_payee", line.get("payee"))
        correct_amount = int(payload.get("correct_amount", payload.get("amount", line.get("amount", 0))))
        return self._make_correction(
            settlement,
            line,
            correct_payee,
            correct_amount,
            at=payload.get("at"),
            reason=payload.get("reason", "manual post-settlement correction"),
        )

    # Common read-model aliases used by different adapter/test conventions.
    get_customer = customer_view
    get_customer_view = customer_view
    get_merchant = merchant_view
    get_merchant_view = merchant_view
    get_store = merchant_view
    get_point = point_view
    get_box = box_view
    get_box_trace = box_view
    get_issue_details = issue_view
    get_issue_view = issue_view
    settle_period = settle_month
    monthly_settlement = settle_month


def sample_service() -> TurnBoxService:
    """Convenience constructor used by demos and smoke tests."""
    root = Path(__file__).resolve().parents[1] / "samples"
    svc = TurnBoxService()
    svc.load_directory(root)
    return svc


def create_service(db_path: str | Path | None = None, *, samples: bool = False) -> TurnBoxService:
    """Create an in-memory or JSON-backed service."""
    svc = TurnBoxService.load(db_path) if db_path else TurnBoxService()
    if samples and not svc.issues:
        root = Path(__file__).resolve().parents[1] / "samples"
        svc.load_directory(root)
        if db_path:
            svc.save(db_path)
    return svc


def load_exchange(directory: str | Path) -> TurnBoxService:
    """Load an exchange directory into a fresh service."""
    svc = TurnBoxService()
    svc.load_directory(directory)
    return svc


def process_directory(directory: str | Path) -> dict[str, Any]:
    """Load an exchange directory and return the common operational reports."""
    svc = load_exchange(directory)
    periods = sorted({line["period"] for line in svc.settlement_lines()})
    return {
        "summary": svc.admin_overview()["counts"],
        "disputes": svc.list_disputes(),
        "boxes": [svc.box_view(box) for box in sorted({r.get("box") for r in svc.issues.values()})],
        "settlement_lines": svc.settlement_lines(),
        "periods": {period: svc.settlement_lines(period) for period in periods},
        "integrity": svc.integrity_report(),
    }

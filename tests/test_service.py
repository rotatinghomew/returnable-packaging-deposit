"""Business-rule tests for the shared turnbox service."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from turnbox.errors import BusinessRuleError, ConflictError
from turnbox.server import build_server
from turnbox.service import TurnBoxService

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def load_samples() -> TurnBoxService:
    svc = TurnBoxService()
    svc.load_directory(SAMPLES)
    return svc


def base_service() -> TurnBoxService:
    svc = TurnBoxService()
    svc.ingest_many(
        [
            {"policy": "p1", "deduction": 300, "collection_fee": 120},
        ],
        "policies",
    )
    svc.ingest_many(
        [
            {"point": "store-a", "merchant": "A"},
            {"point": "store-c", "merchant": "C"},
            {"point": "machine-c", "merchant": "C"},
            {"point": "store-d", "merchant": "D"},
        ],
        "points",
    )
    return svc


class SampleScenarioTest(unittest.TestCase):
    def setUp(self):
        self.svc = load_samples()

    def test_duplicate_exchange_row_is_idempotent(self):
        # samples/collections.ndjson contains the auto-4/m1 receipt twice.
        self.assertEqual(4, len(self.svc.collections))
        result = self.svc.ingest(
            {
                "point": "auto-4",
                "event": "m1",
                "kind": "machine_in",
                "issue": "rent1",
                "box": "box7",
                "occurred_at": "2026-11-17T18:00:00+08:00",
            },
            "collections",
        )
        self.assertEqual("duplicate", result["status"])
        self.assertEqual(4, len(self.svc.collections))

    def test_machine_chain_uses_actual_receipt_time_and_exact_deadline(self):
        check = self.svc.get_collection("hot-c", "c1")
        view = self.svc.collection_view(check)
        self.assertTrue(view["prior_valid"])
        self.assertEqual("2026-11-17T18:00:00+08:00", view["effective_at"])
        rent1 = self.svc.issue_view("rent1")
        self.assertEqual("2026-11-10T18:00:00+08:00", rent1["timing"]["issued_at"])
        self.assertEqual("2026-11-17T18:00:00+08:00", rent1["deadline_at"])

    def test_customer_sees_each_box_refund_and_dispute(self):
        guest = self.svc.customer_view("guest-a")
        self.assertEqual({"rent1", "rent3"}, {x["issue"] for x in guest["issues"]})
        by_issue = {x["issue"]: x for x in guest["boxes"]}
        self.assertEqual("settled", by_issue["rent1"]["refund_status"])
        self.assertEqual(1800, by_issue["rent1"]["settled_amount"])
        self.assertIsNotNone(by_issue["rent1"]["dispute_id"])
        self.assertIsNone(by_issue["rent3"]["dispute_id"])
        self.assertEqual(1, len(guest["disputes"]))
        dispute = guest["disputes"][0]
        self.assertEqual({"hot-c", "hot-d"}, set(dispute["merchants"]))
        self.assertIn("guest-a", dispute["customers"])

    def test_new_lease_deposit_is_independent_of_old_refund(self):
        rent1 = self.svc.issue_view("rent1")
        rent2 = self.svc.issue_view("rent2")
        self.assertEqual(1800, rent1["deposit"])
        self.assertEqual("settled", rent1["refund"]["status"])
        self.assertEqual(1900, rent2["deposit"])
        self.assertEqual(1900, rent2["amounts"]["refund_due"])
        self.assertEqual("not_requested", rent2["refund"]["status"])
        self.assertTrue(rent2["accounts"]["segregated"])

    def test_box_tracking_distinguishes_pending_cleaning_and_wash_failure(self):
        box7 = self.svc.box_view("box7")
        self.assertEqual("pending_cleaning", box7["status"])
        self.assertEqual("hot-c", box7["custodian"])
        self.assertFalse(box7["rentable"])
        self.assertEqual("disp-rent1", box7["open_dispute_id"])

        box8 = self.svc.box_view("box8")
        self.assertEqual("wash_failed", box8["status"])
        self.assertFalse(box8["rentable"])

    def test_monthly_lines_protect_disputed_fee_and_include_valid_fee(self):
        lines = {line["issue"]: line for line in self.svc.settlement_lines("2026-11")}
        self.assertEqual("held", lines["rent1"]["status"])
        self.assertEqual(("hot-a", "hot-c"), (lines["rent1"]["payer"], lines["rent1"]["payee"]))
        self.assertTrue(self.svc.issue_view("rent1")["timing"]["exact_deadline"])
        self.assertEqual("ready", lines["rent2"]["status"])
        self.assertEqual(175, lines["rent2"]["amount"])
        self.assertEqual(("hot-b", "hot-c"), (lines["rent2"]["payer"], lines["rent2"]["payee"]))

    def test_store_can_trace_deposit_refund_and_collection_for_other_merchant(self):
        hot_c = self.svc.merchant_view("hot-c")
        other = {(x["issue"], x["original_merchant"]) for x in hot_c["collections_on_behalf_of_other_stores"]}
        self.assertIn(("rent1", "hot-a"), other)
        self.assertIn(("rent2", "hot-b"), other)
        rent1_business = self.svc.issue_view("rent1")
        self.assertEqual(1800, rent1_business["accounts"]["deposit_received"])
        self.assertEqual(1800, rent1_business["accounts"]["refund_settled"])


class RefundEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.svc = base_service()
        self.svc.ingest(
            {
                "issue": "i1",
                "box": "b1",
                "merchant": "A",
                "customer": "u1",
                "policy": "p1",
                "deposit": 1000,
                "issued_at": "2026-11-01T10:00:00+08:00",
            },
            "issues",
        )

    def test_machine_receipt_alone_cannot_refund(self):
        self.svc.ingest(
            {
                "point": "machine-c",
                "event": "m1",
                "kind": "machine_in",
                "issue": "i1",
                "box": "b1",
                "occurred_at": "2026-11-08T09:00:00+08:00",
            },
            "collections",
        )
        view = self.svc.issue_view("i1")
        self.assertEqual("awaiting_box_check", view["eligibility"]["status"])
        self.assertFalse(view["eligibility"]["new_refund_allowed"])
        with self.assertRaises(BusinessRuleError):
            self.svc.create_refund("i1")

    def test_machine_receipt_cannot_become_next_lease_refund_proof(self):
        svc = base_service()
        for issue_id, issued in [
            ("old", "2026-11-01T10:00:00+08:00"),
            ("next", "2026-11-09T10:00:00+08:00"),
        ]:
            svc.ingest(
                {"issue": issue_id, "box": "same-qr", "merchant": "A", "customer": "u",
                 "policy": "p1", "deposit": 1000, "issued_at": issued},
                "issues",
            )
        # A temporary machine row from the old collection has not been checked.
        svc.ingest(
            {"point": "machine-c", "event": "old-m", "kind": "machine_in", "issue": "old",
             "box": "same-qr", "occurred_at": "2026-11-08T11:00:00+08:00"},
            "collections",
        )
        next_view = svc.issue_view("next")
        self.assertEqual("not_returned", next_view["eligibility"]["status"])
        self.assertIsNone(next_view["return_basis"])
        old_view = svc.issue_view("old")
        self.assertEqual("awaiting_box_check", old_view["eligibility"]["status"])

        # Even if the machine row is mislabelled as the next issue, a timestamp
        # before that lease starts prevents it from matching.
        svc.ingest(
            {"point": "machine-c", "event": "wrong", "kind": "machine_in", "issue": "next",
             "box": "same-qr", "occurred_at": "2026-11-08T12:00:00+08:00"},
            "collections",
        )
        wrong = [c for c in svc.issue_view("next")["collections"] if c["event"] == "wrong"][0]
        self.assertFalse(wrong["matches_lease"])

    def test_unsettled_refund_does_not_block_clean_reissue(self):
        svc = base_service()
        svc.ingest(
            {"issue": "old", "box": "flow-box", "merchant": "A", "customer": "u", "policy": "p1",
             "deposit": 1000, "issued_at": "2026-11-01T10:00:00+08:00"},
            "issues",
        )
        svc.ingest(
            {"point": "store-c", "event": "c", "kind": "confirmed", "issue": "old", "box": "flow-box",
             "occurred_at": "2026-11-02T10:00:00+08:00"},
            "collections",
        )
        refund = svc.create_refund("old")
        ref = refund["refund"]["channels"][0]["channel_ref"]
        svc.transition_refund(ref, "accepted")  # not settled yet
        svc.ingest(
            {"move": "wash", "box": "flow-box", "from": "C", "to": "wash-1", "kind": "handoff",
             "occurred_at": "2026-11-02T12:00:00+08:00"},
            "moves",
        )
        svc.ingest(
            {"move": "ok", "box": "flow-box", "from": "wash-1", "to": "A", "kind": "wash_ok",
             "occurred_at": "2026-11-03T08:00:00+08:00"},
            "moves",
        )
        issue = svc.create_issue(
            {"issue": "new", "box": "flow-box", "merchant": "A", "customer": "u2", "policy": "p1",
             "deposit": 1000, "issued_at": "2026-11-03T10:00:00+08:00"}
        )
        self.assertEqual("new", issue["issue_id"])
        self.assertEqual("accepted", svc.issue_view("old")["refund"]["status"])

    def test_old_lease_recovery_or_supplement_is_issue_scoped(self):
        svc = base_service()
        for issue_id in ("old", "new"):
            svc.ingest(
                {"issue": issue_id, "box": f"box-{issue_id}", "merchant": "A", "customer": "u",
                 "policy": "p1", "deposit": 1000, "issued_at": "2026-11-01T10:00:00+08:00"},
                "issues",
            )
        svc.add_issue_entry("old", {"kind": "recovery", "amount": 250, "reason": "late damage verification"})
        svc.add_issue_entry("old", {"kind": "supplementary_refund", "amount": 50})
        old = svc.issue_view("old")
        new = svc.issue_view("new")
        self.assertEqual(250, old["amounts"]["recovery_due"])
        self.assertEqual(50, old["amounts"]["supplementary_refund_due"])
        self.assertTrue(old["accounts"]["new_lease_deposit_protected"])
        self.assertEqual(1000, new["deposit"])
        self.assertEqual(0, new["amounts"]["recovery_due"])
        self.assertEqual(0, new["amounts"]["supplementary_refund_due"])

    def test_late_return_uses_issue_policy_deduction(self):
        self.svc.ingest(
            {
                "point": "store-c",
                "event": "c1",
                "kind": "confirmed",
                "issue": "i1",
                "box": "b1",
                "occurred_at": "2026-11-08T10:00:01+08:00",
            },
            "collections",
        )
        view = self.svc.issue_view("i1")
        self.assertEqual("eligible_late_deduction", view["eligibility"]["status"])
        self.assertEqual(300, view["amounts"]["deduction"])
        self.assertEqual(700, view["amounts"]["refund_due"])
        result = self.svc.create_refund("i1")
        self.assertEqual("submitted", result["refund"]["channels"][0]["state"])
        self.svc.transition_refund(result["refund"]["channels"][0]["channel_ref"], "accepted")
        accepted = self.svc.issue_view("i1")["refund"]
        self.assertEqual("accepted", accepted["status"])
        self.svc.transition_refund(accepted["channels"][0]["channel_ref"], "settled")
        settled = self.svc.issue_view("i1")["refund"]
        self.assertEqual("settled", settled["status"])
        self.assertTrue(settled["completed"])

    def test_normal_wear_or_wash_failure_does_not_reopen_refund_amount(self):
        self.svc.ingest(
            {
                "point": "store-c",
                "event": "c1",
                "kind": "confirmed",
                "issue": "i1",
                "box": "b1",
                "occurred_at": "2026-11-08T09:00:00+08:00",
            },
            "collections",
        )
        created = self.svc.create_refund("i1")
        ref = created["refund"]["channels"][0]["channel_ref"]
        self.svc.transition_refund(ref, "settled")
        self.svc.ingest(
            {"move": "wash-fail-1", "box": "b1", "from": "wash-1", "to": "A", "kind": "wash_fail",
             "occurred_at": "2026-11-09T10:00:00+08:00"},
            "moves",
        )
        view = self.svc.issue_view("i1")
        self.assertEqual(0, view["amounts"]["deduction"])
        self.assertEqual(1000, view["amounts"]["refund_due"])
        self.assertEqual("settled", view["refund"]["status"])
        self.assertEqual("wash_failed", self.svc.box_view("b1")["status"])


class MultiBoxAndAvailabilityTest(unittest.TestCase):
    def test_single_box_return_leaves_sibling_independent(self):
        svc = base_service()
        svc.create_order(
            {
                "order_id": "order-9",
                "customer": "cust-9",
                "merchant": "A",
                "policy": "p1",
                "boxes": ["box-a", "box-b"],
                "deposit_per_box": 500,
                "issued_at": "2026-11-01T10:00:00+08:00",
            }
        )
        svc.ingest(
            {"point": "store-c", "event": "only-one", "kind": "confirmed", "issue": "order-9-01",
             "box": "box-a", "occurred_at": "2026-11-02T10:00:00+08:00"},
            "collections",
        )
        customer = svc.customer_view("cust-9")
        statuses = {x["issue"]: x["eligibility_status"] for x in customer["boxes"]}
        self.assertEqual("eligible_full", statuses["order-9-01"])
        self.assertEqual("not_returned", statuses["order-9-02"])

    def test_only_wash_ok_box_can_be_reissued(self):
        svc = base_service()
        svc.ingest(
            {"issue": "old", "box": "clean-box", "merchant": "A", "customer": "u", "policy": "p1",
             "deposit": 10, "issued_at": "2026-11-01T10:00:00+08:00"},
            "issues",
        )
        svc.ingest(
            {"point": "store-c", "event": "c", "kind": "confirmed", "issue": "old", "box": "clean-box",
             "occurred_at": "2026-11-02T10:00:00+08:00"},
            "collections",
        )
        svc.ingest(
            {"move": "out", "box": "clean-box", "from": "C", "to": "wash-1", "kind": "handoff",
             "occurred_at": "2026-11-02T12:00:00+08:00"},
            "moves",
        )
        self.assertEqual("pending_cleaning", svc.box_view("clean-box")["status"])
        with self.assertRaises(BusinessRuleError):
            svc.create_issue(
                {"issue": "new-bad", "box": "clean-box", "merchant": "A", "customer": "u2",
                 "policy": "p1", "deposit": 10, "issued_at": "2026-11-03T09:00:00+08:00"}
            )
        svc.ingest(
            {"move": "ok", "box": "clean-box", "from": "wash-1", "to": "A", "kind": "wash_ok",
             "handoff": "out", "occurred_at": "2026-11-03T08:00:00+08:00"},
            "moves",
        )
        self.assertTrue(svc.box_view("clean-box")["rentable"])
        view = svc.create_issue(
            {"issue": "new-good", "box": "clean-box", "merchant": "A", "customer": "u2",
             "policy": "p1", "deposit": 10, "issued_at": "2026-11-03T10:00:00+08:00"}
        )
        self.assertEqual("new-good", view["issue_id"])


class SettlementCorrectionTest(unittest.TestCase):
    def test_correction_points_to_original_settlement_and_protects_next_lease(self):
        svc = base_service()
        svc.ingest(
            {"issue": "old", "box": "b", "merchant": "A", "customer": "u", "policy": "p1",
             "deposit": 1000, "issued_at": "2026-10-01T10:00:00+08:00"},
            "issues",
        )
        svc.ingest(
            {"point": "store-c", "event": "e", "kind": "confirmed", "issue": "old", "box": "b",
             "occurred_at": "2026-10-02T10:00:00+08:00"},
            "collections",
        )
        october = svc.settle_month("2026-10", "2026-11-01T00:00:00+08:00")
        self.assertEqual("finalized", october["status"])
        self.assertEqual("C", october["balances"][0]["payee"])

        # A late report says D also has the same physical box.
        svc.ingest(
            {"point": "store-d", "event": "x", "kind": "confirmed", "issue": "old", "box": "b",
             "occurred_at": "2026-10-02T09:00:00+08:00"},
            "collections",
        )
        dispute = svc.resolve_dispute(
            "disp-old",
            {"claim": "store-d/x", "resolved_by": "admin-7", "at": "2026-11-02T10:00:00+08:00"},
        )
        self.assertEqual("resolved", dispute["status"])
        adjustment_id = dispute["resolution"]["correction_ids"][0]
        adjustment = svc.adjustments[adjustment_id]
        self.assertEqual("STL-2026-10", adjustment["original_settlement_id"])
        self.assertEqual("fee-2026-10-old", adjustment["original_line_id"])
        self.assertEqual("C", adjustment["original_payee"])
        self.assertEqual("D", adjustment["correct_payee"])
        self.assertEqual("issue:old", adjustment["lease_scope"])
        self.assertTrue(adjustment["next_lease_collection_point_protected"])

        # Next lease collected by E must still settle its own full fee.
        svc.ingest(
            {"issue": "next", "box": "b2", "merchant": "A", "customer": "u2", "policy": "p1",
             "deposit": 1000, "issued_at": "2026-11-01T10:00:00+08:00"},
            "issues",
        )
        svc.points["store-e"] = {"point": "store-e", "merchant": "E"}
        svc.ingest(
            {"point": "store-e", "event": "n", "kind": "confirmed", "issue": "next", "box": "b2",
             "occurred_at": "2026-11-03T10:00:00+08:00"},
            "collections",
        )
        november = svc.settle_month("2026-11")
        self.assertEqual(1, len(november["lines"]))
        self.assertEqual("next", november["lines"][0]["issue"])
        self.assertEqual("E", november["lines"][0]["payee"])
        self.assertEqual(120, november["lines"][0]["amount"])
        self.assertNotIn("old", json.dumps(november["lines"]))

    def test_conflicting_same_identity_payload_is_rejected(self):
        svc = base_service()
        record = {"point": "store-c", "event": "x", "kind": "confirmed", "issue": "i", "box": "b",
                  "occurred_at": "2026-11-02T10:00:00+08:00"}
        svc.ingest(record, "collections")
        with self.assertRaises(ConflictError):
            svc.ingest({**record, "box": "other-box"}, "collections")


class PersistenceAndHttpTest(unittest.TestCase):
    def test_state_roundtrip(self):
        svc = load_samples()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            svc.save(path)
            reloaded = TurnBoxService.load(path)
            self.assertEqual(svc.admin_overview()["counts"], reloaded.admin_overview()["counts"])
            self.assertEqual(
                svc.customer_view("guest-a")["boxes"][0]["refund_status"],
                reloaded.customer_view("guest-a")["boxes"][0]["refund_status"],
            )

    def test_http_customer_endpoint(self):
        httpd = build_server("127.0.0.1", 0, load_samples=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/api/boxes/box7", timeout=5) as response:
                data = json.load(response)
            self.assertEqual(200, response.status)
            self.assertEqual("box7", data["box"])
            self.assertFalse(data["rentable"])

            req = Request(
                f"http://127.0.0.1:{port}/api/settlements",
                data=json.dumps({"period": "2026-11", "finalize": False}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as response:
                settlement = json.load(response)
            self.assertEqual(201, response.status)
            self.assertEqual("2026-11", settlement["period"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

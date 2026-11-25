"""核对交换资料的客户编号、时序和关联条件。"""
import json
from pathlib import Path
import unittest


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def rows(name):
    content = (SAMPLES / f"{name}.ndjson").read_text(encoding="utf-8")
    return [json.loads(line) for line in content.splitlines()]


class ExchangeSampleTest(unittest.TestCase):
    def test_records_are_json_objects(self):
        files = sorted(SAMPLES.glob("*.ndjson"))
        self.assertEqual(len(files), 6)
        for path in files:
            with self.subTest(path=path.name):
                records = rows(path.stem)
                self.assertTrue(records)
                self.assertTrue(all(isinstance(row, dict) for row in records))

    def test_related_records_preserve_source_evidence(self):
        from datetime import datetime, timedelta

        first, second, other = rows("issues")
        receipts = rows("collections")
        deadline = datetime.fromisoformat(first["issued_at"]) + timedelta(days=7)
        self.assertEqual(deadline, datetime.fromisoformat(receipts[0]["occurred_at"]))
        self.assertLess(deadline, datetime.fromisoformat(receipts[1]["occurred_at"]))
        self.assertEqual(first["box"], second["box"])
        self.assertNotEqual(first["issue"], second["issue"])
        self.assertNotEqual(first["box"], other["box"])
        self.assertLess(second["issued_at"], rows("refunds")[-1]["occurred_at"])

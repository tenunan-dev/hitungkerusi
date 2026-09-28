"""P2.2 verdict-line parser tests — port fidelity + round-trip against the
real verdicts.jsonl (the file `_compact_verdict` in V2 was the only parser
for; V2 read-only, ported into data/scripts/import/parse_verdicts.py).

Run from the repository root:
    python3 -m pytest data/tests/test_p2_2_verdicts.py -q
"""
import importlib.util
import pathlib
import unittest

IMPORT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "import"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VERDICTS_PATH = (REPO_ROOT / "data" / "canonical" / "research" / "trackers"
                 / "ge16-news-backfill-verdicts.jsonl")

spec = importlib.util.spec_from_file_location("parse_verdicts", IMPORT_DIR / "parse_verdicts.py")
parse_verdicts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parse_verdicts)


class ParseCompactVerdictTests(unittest.TestCase):
    def test_three_field_form_defaults(self):
        row = parse_verdicts.parse_compact_verdict("3:12:1")
        self.assertEqual(row, {"b": 3, "i": 12, "a": True, "c": "", "g": [], "p": [],
                               "s": [], "l": "en", "score": 0.6})

    def test_reject_form(self):
        row = parse_verdicts.parse_compact_verdict("1:0:0")
        self.assertFalse(row["a"])

    def test_nine_field_full_form(self):
        row = parse_verdicts.parse_compact_verdict(
            "2:5:1:election:PH+PN:DAP+BERSATU:Melaka+Johor:ms:7")
        self.assertEqual(row, {"b": 2, "i": 5, "a": True, "c": "election",
                               "g": ["PH", "PN"], "p": ["DAP", "BERSATU"],
                               "s": ["Melaka", "Johor"], "l": "ms", "score": 0.7})

    def test_empty_list_fields_use_dash(self):
        row = parse_verdicts.parse_compact_verdict("1:1:1:legal:-:-:-:ms:0.65")
        self.assertEqual(row["g"], [])
        self.assertEqual(row["p"], [])
        self.assertEqual(row["s"], [])
        self.assertEqual(row["score"], 0.65)

    def test_bare_tenths_score_scaling(self):
        # a bare score is tenths: 7 -> 0.7, 10 -> 1.0 (V2 _compact_verdict)
        self.assertEqual(parse_verdicts.parse_compact_verdict("1:1:1:-:-:-:-:en:7")["score"], 0.7)
        self.assertEqual(parse_verdicts.parse_compact_verdict("1:1:1:-:-:-:-:en:10")["score"], 1.0)

    def test_decimal_score_round_trips(self):
        self.assertEqual(parse_verdicts.parse_compact_verdict("1:1:1:-:-:-:-:en:0.65")["score"], 0.65)

    def test_wrong_field_count_is_none(self):
        self.assertIsNone(parse_verdicts.parse_compact_verdict("1:1:1:election"))
        self.assertIsNone(parse_verdicts.parse_compact_verdict("nonsense"))

    def test_non_numeric_batch_or_index_is_none(self):
        self.assertIsNone(parse_verdicts.parse_compact_verdict("x:1:0"))
        self.assertIsNone(parse_verdicts.parse_compact_verdict("1:y:0"))

    def test_bad_score_falls_back_to_default(self):
        self.assertEqual(parse_verdicts.parse_compact_verdict("1:1:1:-:-:-:-:en:zz")["score"], 0.6)


class RoundTripTests(unittest.TestCase):
    """emit(parse(line)) must re-parse to the identical dict (field-by-field)."""

    SYNTHETIC = [
        "1:0:0",
        "9:19:1",
        "2:5:1:election:PH+PN:DAP+BERSATU:Melaka+Johor:ms:7",
        "3:3:0:legal:-:-:-:en:10",
        "4:7:1:coalition:BN:UMNO+MCA:-:ms:55",
        "5:0:1:analysis:GRS:PBS:Kinabatangan:en:8",
        "6:2:1:poll:-:-:-:-:0.65",
    ]

    def test_synthetic_round_trip_field_by_field(self):
        for line in self.SYNTHETIC:
            with self.subTest(line=line):
                row = parse_verdicts.parse_compact_verdict(line)
                self.assertIsNotNone(row, line)
                reparsed = parse_verdicts.parse_compact_verdict(
                    parse_verdicts.emit_compact_verdict(row))
                self.assertEqual(row, reparsed)

    def test_real_file_round_trip(self):
        if not VERDICTS_PATH.exists():
            self.skipTest("real verdicts.jsonl not present")
        lines = []
        for line in VERDICTS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
        self.assertGreater(len(lines), 100)
        for line in lines:
            with self.subTest(line=line):
                row = parse_verdicts.parse_compact_verdict(line)
                self.assertIsNotNone(row, line)
                reparsed = parse_verdicts.parse_compact_verdict(
                    parse_verdicts.emit_compact_verdict(row))
                self.assertEqual(row, reparsed)

    def test_load_verdicts_real_file(self):
        if not VERDICTS_PATH.exists():
            self.skipTest("real verdicts.jsonl not present")
        rows, bad = parse_verdicts.load_verdicts(VERDICTS_PATH)
        self.assertEqual(bad, 0)
        self.assertEqual(len(rows), 420)  # P2.1 recomputed figure
        self.assertTrue(all(isinstance(key, tuple) for key in rows))


if __name__ == "__main__":
    unittest.main()

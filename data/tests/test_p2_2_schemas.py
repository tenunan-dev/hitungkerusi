"""P2.2 schema tests: the ge16.*.v1 family validates its fixtures and rejects
malformed rows. Event/link schemas are validated against fixtures now and
populated in P2.8.

Run from the repository root:
    python3 -m pytest data/tests/test_p2_2_schemas.py -q
"""
import copy
import json
import pathlib
import unittest

import jsonschema

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "data" / "scripts" / "schemas"
FIXTURE_DIR = REPO_ROOT / "data" / "tests" / "fixtures" / "p2_2"

SCHEMA_FILES = {
    "ge16.evidence.v1": "ge16_evidence.schema.json",
    "ge16.judgment.v1": "ge16_judgment.schema.json",
    "ge16.entity.v1": "ge16_entity.schema.json",
    "ge16.entity-candidate.v1": "ge16_entity-candidate.schema.json",
    "ge16.link.v1": "ge16_link.schema.json",
    "ge16.event.v1": "ge16_event.schema.json",
    "ge16.edition.v1": "ge16_edition.schema.json",
}
FIXTURES = {
    "ge16.evidence.v1": ["evidence_news.json", "evidence_tracker_note.json"],
    "ge16.judgment.v1": ["judgment_batch.json", "judgment_orphan.json",
                         "judgment_corpus_inline.json"],
    "ge16.entity.v1": ["entity.json", "entity_approved.json"],
    "ge16.entity-candidate.v1": ["entity_candidate.json"],
    "ge16.link.v1": ["link.json"],
    "ge16.event.v1": ["event.json"],
    "ge16.edition.v1": ["edition.json"],
}


def load_schema(name):
    with open(SCHEMA_DIR / SCHEMA_FILES[name], encoding="utf-8") as handle:
        return json.load(handle)


class SchemaFileTests(unittest.TestCase):
    def test_all_seven_schema_files_exist_and_parse(self):
        for title, filename in SCHEMA_FILES.items():
            with self.subTest(schema=title):
                document = load_schema(title)
                self.assertEqual(document["$schema"],
                                 "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(document["title"], title)

    def test_fixtures_validate(self):
        for title, fixtures in FIXTURES.items():
            schema = load_schema(title)
            for fixture in fixtures:
                with self.subTest(schema=title, fixture=fixture):
                    path = FIXTURE_DIR / fixture
                    if not path.exists():
                        self.fail(f"missing fixture: {path}")
                    with open(path, encoding="utf-8") as handle:
                        row = json.load(handle)
                    jsonschema.validate(row, schema)

    def test_schema_files_are_valid_json_tool_output(self):
        for filename in SCHEMA_FILES.values():
            with open(SCHEMA_DIR / filename, encoding="utf-8") as handle:
                text = handle.read()
            # python3 -m json.tool equivalence: strict parse + round-trip
            self.assertEqual(json.loads(json.dumps(json.loads(text))), json.loads(text))


class NegativeTests(unittest.TestCase):
    def test_evidence_rejects_bad_id_and_kind(self):
        schema = load_schema("ge16.evidence.v1")
        with open(FIXTURE_DIR / "evidence_news.json", encoding="utf-8") as handle:
            row = json.load(handle)
        bad_id = copy.deepcopy(row)
        bad_id["evidence_id"] = "EV0123456789abcdef"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad_id, schema)
        bad_kind = copy.deepcopy(row)
        bad_kind["kind"] = "rumour"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad_kind, schema)

    def test_evidence_rejects_unknown_top_level_property(self):
        schema = load_schema("ge16.evidence.v1")
        with open(FIXTURE_DIR / "evidence_news.json", encoding="utf-8") as handle:
            row = json.load(handle)
        row["secret_extra"] = 1
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(row, schema)

    def test_judgment_requires_verifiable_basis(self):
        schema = load_schema("ge16.judgment.v1")
        with open(FIXTURE_DIR / "judgment_orphan.json", encoding="utf-8") as handle:
            row = json.load(handle)
        row["basis"].pop("verifiable")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(row, schema)

    def test_judgment_rejects_unknown_origin(self):
        schema = load_schema("ge16.judgment.v1")
        with open(FIXTURE_DIR / "judgment_batch.json", encoding="utf-8") as handle:
            row = json.load(handle)
        row["basis"]["origin"] = "guessed"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(row, schema)

    def test_judgment_batch_requires_dual_hash_basis(self):
        """P2.2-R1: origin judged-batch must carry the auditor-verifiable
        batch_file_sha256 plus the documented embedded hash and semantics."""
        schema = load_schema("ge16.judgment.v1")
        with open(FIXTURE_DIR / "judgment_batch.json", encoding="utf-8") as handle:
            row = json.load(handle)
        for field in ("batch_file_sha256", "embedded_source_sha256",
                      "source_hash_semantics"):
            missing = copy.deepcopy(row)
            missing["basis"].pop(field)
            with self.subTest(missing=field):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(missing, schema)

    def test_edition_requires_utc_compact_id(self):
        schema = load_schema("ge16.edition.v1")
        with open(FIXTURE_DIR / "edition.json", encoding="utf-8") as handle:
            row = json.load(handle)
        row["edition_id"] = "2026-09-28T12:00:00+00:00"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(row, schema)

    def test_candidate_status_enum(self):
        schema = load_schema("ge16.entity-candidate.v1")
        with open(FIXTURE_DIR / "entity_candidate.json", encoding="utf-8") as handle:
            row = json.load(handle)
        row["status"] = "auto-promoted"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(row, schema)


if __name__ == "__main__":
    unittest.main()

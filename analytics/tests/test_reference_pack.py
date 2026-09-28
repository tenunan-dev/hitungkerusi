"""Contract tests for the GE16 reference pack (the deterministic claim ledger).

Hermetic and LLM-free: the pack is built from the same canonical inputs the
deterministic builders read, and NOTHING is written into the repository — the
only file write goes to a temp directory. The reference pack is the sole source
of facts the AI-authored edition may cite, so these tests fix its shape:
determinism, unique intra-pack ids, the family inventory, and the boundary
between "a citable token" and "structure".
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENGINE = REPOSITORY_ROOT / "02_FORECAST" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import reference_pack as rp  # noqa: E402

FEDERAL = {}
KEDAH = {}

#: Families the authored sections are built from — a missing one silently
#: starves a section of facts, so the inventory is asserted, not assumed.
REQUIRED_FEDERAL_FAMILIES = {
    "meta", "projection", "bloc", "macro", "threshold", "vacancy", "event_shock",
    "flip", "seat", "seat_type_count", "watch", "story", "story_summary",
    "signal", "scenario", "swing", "electorate", "state_seat",
}
REQUIRED_STATE_FAMILIES = {
    "meta", "projection", "bloc", "macro", "threshold", "vacancy", "event_shock",
    "flip", "seat", "watch", "story", "story_summary", "signal", "scenario",
    "swing", "electorate",
}
SECTION_KEYS = [
    "intro_context", "this_week", "electorate", "electorate_dynamics",
    "story_threads_intro", "story_threads_items", "scenario_narrative",
    "watch_list", "closing",
]


def setUpModule():
    FEDERAL.clear()
    FEDERAL.update(rp.build_federal_pack())
    KEDAH.clear()
    KEDAH.update(rp.build_state_pack("Kedah"))


def canonical(pack):
    return json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PackDeterminismTests(unittest.TestCase):
    def test_two_runs_are_byte_identical(self):
        first = rp.build_federal_pack()
        second = rp.build_federal_pack()
        self.assertEqual(canonical(first), canonical(second))
        self.assertEqual(first["pack_hash"], second["pack_hash"])

    def test_written_pack_round_trips_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = rp.write_pack(FEDERAL, out_path=str(Path(tmp) / "pack.json"))
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            self.assertEqual(json.loads(body), json.loads(canonical(FEDERAL)))
            # a second write of the same inputs is byte-identical
            again = rp.write_pack(FEDERAL, out_path=str(Path(tmp) / "pack2.json"))
            with open(again, encoding="utf-8") as handle:
                self.assertEqual(body, handle.read())

    def test_state_pack_is_deterministic_too(self):
        self.assertEqual(canonical(rp.build_state_pack("Kedah")), canonical(KEDAH))

    def test_pack_hash_binds_the_content(self):
        self.assertEqual(FEDERAL["pack_hash"], rp.pack_hash(FEDERAL))
        mutated = json.loads(canonical(FEDERAL))
        mutated["claims"][0]["value_str"] = "999999"
        self.assertNotEqual(rp.pack_hash(mutated), FEDERAL["pack_hash"])

    def test_filename_carries_date_lang_and_scope(self):
        self.assertEqual(rp.pack_filename(FEDERAL),
                         "GE16-reference-{0}-en.json".format(FEDERAL["as_of"]))
        self.assertEqual(rp.pack_filename(KEDAH),
                         "GE16-reference-{0}-en-dun-kedah.json".format(KEDAH["as_of"]))


class ClaimShapeTests(unittest.TestCase):
    def test_claim_ids_are_unique_in_every_pack(self):
        for pack in (FEDERAL, KEDAH):
            ids = [claim["claim_id"] for claim in pack["claims"]]
            self.assertEqual(len(ids), len(set(ids)), "duplicate claim_id")
            self.assertTrue(all(ids), "empty claim_id")

    def test_every_claim_carries_value_unit_pointer_and_id(self):
        for claim in FEDERAL["claims"]:
            for field in ("claim_id", "family", "key", "value", "value_str", "unit",
                          "label", "as_of", "source"):
                self.assertIn(field, claim)
            self.assertNotEqual(claim["value_str"], "")
            self.assertTrue(claim["as_of"], claim["claim_id"])
            self.assertTrue(claim["source"], claim["claim_id"])

    def test_claim_id_encodes_its_own_rendered_value(self):
        # fed:P50:139 — the id's last segment is the quotable value, which is
        # what makes a citation self-checking.
        claim = rp.claim_index(FEDERAL)["fed:P50:139"]
        self.assertEqual(claim["value"], 139)
        self.assertEqual(claim["value_str"], "139")
        self.assertEqual(claim["key"], "P50")
        self.assertEqual(claim["family"], "projection")

    def test_claim_id_lookup_returns_typed_values(self):
        index = rp.claim_index(FEDERAL)
        self.assertEqual(index["fed:bloc:PN:81"]["value"], 81)
        self.assertEqual(index["fed:threshold:simple_majority:112"]["unit"], "seats")
        self.assertEqual(index["fed:macro:gdp_yoy:6.0"]["value"], 6.0)

    def test_embedded_values_cannot_break_a_citation(self):
        for claim in FEDERAL["claims"] + KEDAH["claims"]:
            self.assertNotIn("[", claim["claim_id"])
            self.assertNotIn("]", claim["claim_id"])
            self.assertNotIn("\n", claim["claim_id"])


class VolumeAndFamilyTests(unittest.TestCase):
    def test_federal_families_and_claim_volume(self):
        families = set(FEDERAL["families"])
        missing = REQUIRED_FEDERAL_FAMILIES - families
        self.assertFalse(missing, "missing families: {0}".format(sorted(missing)))
        self.assertGreaterEqual(FEDERAL["counts"]["top_level_claims"], 50)
        self.assertGreaterEqual(FEDERAL["counts"]["claims"], 200)

    def test_state_families_and_claim_volume(self):
        families = set(KEDAH["families"])
        missing = REQUIRED_STATE_FAMILIES - families
        self.assertFalse(missing, "missing families: {0}".format(sorted(missing)))
        self.assertGreaterEqual(KEDAH["counts"]["top_level_claims"], 15)
        self.assertEqual(KEDAH["kind"], "state")
        self.assertEqual(KEDAH["state"], "Kedah")

    def test_per_seat_family_covers_the_whole_claimable_universe(self):
        seats = [c for c in FEDERAL["claims"]
                 if c["family"] == "seat" and (c.get("group") or "").startswith("seat:")]
        codes = {c["group"].split(":", 1)[1] for c in seats}
        self.assertEqual(len(codes), 222)
        self.assertEqual(len(FEDERAL["codes"]["seats"]), 222)
        self.assertEqual(FEDERAL["codes"]["seats"], sorted(FEDERAL["codes"]["seats"]))

    def test_state_pack_names_only_that_state_seats(self):
        forecast = json.loads((REPOSITORY_ROOT / "02_FORECAST" / "outputs" / "latest"
                               / "ge16-forecast-latest.json").read_text(encoding="utf-8"))
        kedah = {seat["code"] for seat in forecast["projected_seats"]
                 if seat.get("state") == "Kedah"}
        self.assertTrue(kedah)
        strays = set(KEDAH["codes"]["seats"]) - kedah
        self.assertFalse(strays, "state pack leaked seats: {0}".format(sorted(strays)))


class SectionSpecTests(unittest.TestCase):
    def test_sections_are_the_nine_authoring_keys(self):
        self.assertEqual([s["key"] for s in FEDERAL["sections"]], SECTION_KEYS)

    def test_every_section_has_facts_and_bilingual_briefs(self):
        for pack in (FEDERAL, KEDAH):
            for section in pack["sections"]:
                self.assertTrue(section["claim_ids"], section["key"])
                self.assertTrue(section["title_en"] and section["title_ms"], section["key"])
                self.assertTrue(section["brief_en"] and section["brief_ms"], section["key"])
                index = rp.claim_index(pack)
                for claim_id in section["claim_ids"]:
                    self.assertIn(claim_id, index, "{0}/{1}".format(section["key"], claim_id))

    def test_section_claim_ids_are_unique_within_a_section(self):
        for section in FEDERAL["sections"]:
            ids = section["claim_ids"]
            self.assertEqual(len(ids), len(set(ids)), section["key"])


class CitableTokenTests(unittest.TestCase):
    def test_numbers_dates_and_seats_are_tokenised(self):
        tokens = rp.extract_tokens("A 140 of 222 reading on 24 September 2026 at P015, margin 23.3%.")
        self.assertEqual([t["token"] for t in tokens],
                         ["140", "222", "24 September 2026", "P015", "23.3"])

    def test_structural_numbering_is_not_a_fact(self):
        # House style writes section references as §11.4 / "Section 11.4" and
        # headings as "## 3. ...": none of those are citable facts.
        tokens = rp.extract_tokens(
            "## 3. The Electorate\n\nSee §11.4 and Section 11.4 for detail.\n\n1. First point\n")
        self.assertEqual(tokens, [])

    def test_a_bare_number_is_never_masked(self):
        self.assertEqual([t["token"] for t in rp.extract_tokens("150 seats were won.")],
                         ["150"])

    def test_labels_and_plate_codes_do_not_yield_tokens(self):
        # GE16 / v1.0 / N.15 are names or codes, never bare numbers.
        self.assertEqual([t["kind"] for t in rp.extract_tokens("GE16 model factor-v1.0")], [])

    def test_allowed_tokens_cover_claim_values_aliases_and_codes(self):
        allowed = rp.allowed_tokens(FEDERAL)
        for expected in ("139", "140", "112", "222", "6.0", "4.0816", "2026-09-24",
                         "24 September 2026", "5,000", "P015", "PN"):
            self.assertIn(expected, allowed, expected)
        self.assertNotIn("150", allowed)

    def test_tokens_inside_a_quoted_headline_are_licensed(self):
        # A ledger headline may quote a number ("OPR at 2.75%"); because the
        # headline is a claim value, its tokens are legitimately citable.
        claim = next(c for c in FEDERAL["claims"] if c["family"] == "signal")
        for token in rp.extract_tokens(claim["value_str"]):
            self.assertIn(rp.normalise_token(token["token"], token["kind"]),
                          rp.allowed_tokens(FEDERAL), token["token"])


if __name__ == "__main__":
    unittest.main()

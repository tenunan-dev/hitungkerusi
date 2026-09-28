"""Contract tests for the GE16 AI-authoring harness (offline, no live calls).

The harness exists to let a swappable writer model phrase the forecast while
never letting it invent a fact. These tests pin that contract WITHOUT any model
call: every authoring path here is either ``--offline`` (deterministic pack
deck) or driven by a canned fixture writer. `LiveWriter` is asserted never to be
constructed in offline mode, and its key lookup is tested without a network
round trip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENGINE = REPOSITORY_ROOT / "02_FORECAST" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import author_reports as ar  # noqa: E402
import reference_pack as rp  # noqa: E402

FEDERAL = {}
KEDAH = {}
TMP = ""


class FixtureWriter:
    """A canned writer model: returns fixed text, never touches the network."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def write(self, prompt, system, section=None):
        self.calls.append(section)
        return self.text(section) if callable(self.text) else self.text


class ForbiddenWriter:
    """Fails loudly if a code path reaches for a live writer."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("LiveWriter must not be constructed in offline mode")


def setUpModule():
    global TMP
    FEDERAL.clear()
    FEDERAL.update(rp.build_federal_pack())
    KEDAH.clear()
    KEDAH.update(rp.build_state_pack("Kedah"))
    TMP = tempfile.mkdtemp(prefix="ge16-authoring-test-")
    unittest.addModuleCleanup(shutil.rmtree, TMP, ignore_errors=True)


def paths(name):
    base = Path(TMP) / name
    return str(base / "ai"), str(base / "work"), str(base / "pack")


class OfflineModeTests(unittest.TestCase):
    def test_offline_run_produces_the_deterministic_deck_without_a_writer(self):
        original = ar.LiveWriter
        ar.LiveWriter = ForbiddenWriter
        try:
            out, work, pack_root = paths("offline-deck")
            result = ar.run_authoring(lang="en", offline=True, pack=FEDERAL,
                                      out_root=out, work_root=work,
                                      pack_root=pack_root)
        finally:
            ar.LiveWriter = original
        self.assertEqual(len(result["records"]), len(FEDERAL["sections"]))
        self.assertTrue(all(r["render"] == "offline-fallback" for r in result["records"]))
        self.assertTrue(all(r["attempts"] == 0 for r in result["records"]))
        self.assertEqual(result["manifest_data"]["mode"], "offline")
        self.assertEqual(result["manifest_data"]["writer"]["provider"], "offline")
        self.assertIn("## 1. ", result["markdown"])

    def test_offline_mode_authors_en_and_ms_for_federal_and_one_state(self):
        out_ai, work, pack_root = paths("offline-editions")
        produced = []
        for lang in ("en", "ms"):
            for state in (None, "Kedah"):
                produced.append(ar.run_authoring(
                    lang=lang, state=state, offline=True, out_root=out_ai,
                    work_root=work, pack_root=pack_root))
        for result in produced:
            for path in (result["edition"], result["archive"]):
                self.assertTrue(Path(path).is_file(), path)
                self.assertGreater(Path(path).stat().st_size, 500, path)
        names = {Path(r["edition"]).name for r in produced}
        self.assertEqual(names, {"GE16_Malaysia_General_Election_Report.md",
                                 "GE16_Malaysia_General_Election_Report_MS.md",
                                 "GE16_Kedah_Report.md", "GE16_Kedah_Report_MS.md"})
        en = next(r for r in produced if not Path(r["edition"]).name.endswith("_MS.md"))
        ms = next(r for r in produced if Path(r["edition"]).name.endswith("_MS.md"))
        # No writer ran, so the edition is NOT branded as an AI edition: the
        # title states the mode instead, and the footer states the standing.
        for result, title in ((en, "GE16 — Model Report (deterministic render)"),
                              (ms, "GE16 — Laporan Model (paparan deterministik)")):
            self.assertIn(title, result["markdown"])
            self.assertNotIn("AI-Authored", result["markdown"])
            self.assertNotIn("Ditulis AI", result["markdown"])
            self.assertEqual(result["manifest_data"]["mode"], "offline")
            self.assertEqual(result["manifest_data"]["edition_mode"],
                             "deterministic-render")
            self.assertEqual(result["manifest_data"]["published"], True)

    def test_offline_pack_is_emitted_beside_the_run(self):
        out_ai, work, pack_root = paths("offline-pack")
        result = ar.run_authoring(lang="en", state="Kedah", offline=True,
                                  out_root=out_ai, work_root=work, pack_root=pack_root)
        expected = Path(pack_root) / "GE16-reference-{0}-en-dun-kedah.json".format(
            KEDAH["as_of"])
        self.assertEqual(Path(result["pack_path"]), expected)
        self.assertTrue(expected.is_file())
        self.assertEqual(json.loads(expected.read_text(encoding="utf-8"))["pack_hash"],
                         KEDAH["pack_hash"])

    def test_fallback_text_passes_its_own_verifier(self):
        for pack in (FEDERAL, KEDAH):
            index = rp.claim_index(pack)
            allowed = rp.allowed_tokens(pack)
            for lang in ("en", "ms"):
                for section in pack["sections"]:
                    body = ar.render_fallback(pack, section, index, lang)
                    self.assertTrue(body, section["key"])
                    self.assertEqual(ar.verify_text(body, allowed, index), [],
                                     "{0}/{1}".format(lang, section["key"]))

    def test_fallback_is_deterministic(self):
        index = rp.claim_index(FEDERAL)
        section = FEDERAL["sections"][0]
        self.assertEqual(ar.render_fallback(FEDERAL, section, index, "en"),
                         ar.render_fallback(FEDERAL, section, index, "en"))

    def test_editions_never_target_the_deterministic_files(self):
        edition, archive = ar.output_paths(FEDERAL)
        self.assertTrue(edition.startswith(ar.REPORT_AI))
        self.assertNotEqual(edition, ar.REPORT_AUTHORITY["federal"])
        state_edition, _ = ar.output_paths(KEDAH)
        self.assertTrue(state_edition.startswith(ar.REPORT_AI))
        self.assertNotIn("/latest/", state_edition)
        self.assertIn("/archive/", archive)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.index = rp.claim_index(FEDERAL)
        self.allowed = rp.allowed_tokens(FEDERAL)

    def test_verifier_accepts_pack_quoted_text(self):
        text = ("The model assigns 140 of 222 seats to the government-aligned bloc "
                "[fed:govt_expected:140], against the 112-seat majority "
                "[fed:threshold:simple_majority:112].")
        self.assertEqual(ar.verify_text(text, self.allowed, self.index), [])

    def test_verifier_catches_a_mutated_number(self):
        text = "The model assigns 150 of 222 seats to the government-aligned bloc."
        violations = ar.verify_text(text, self.allowed, self.index)
        self.assertEqual([v["token"] for v in violations], ["150"])
        self.assertEqual(violations[0]["kind"], "number")
        self.assertEqual(violations[0]["line"], 1)

    def test_verifier_catches_an_unknown_seat_code_and_citation(self):
        text = "Seat P999 flips to PN [fed:P999:PN]."
        violations = ar.verify_text(text, self.allowed, self.index)
        self.assertEqual(sorted({v["kind"] for v in violations}), ["citation", "seat"])
        self.assertIn("P999", {v["token"] for v in violations})
        self.assertIn("fed:P999:PN", {v["token"] for v in violations})

    def test_verifier_ignores_headings_and_structural_numbering(self):
        text = "## 4. Electorate\n\n1. First point\n\nSee §11.4 for detail.\n"
        self.assertEqual(ar.verify_text(text, self.allowed, self.index), [])

    def test_mutated_number_triggers_retry_then_degrades_to_the_fallback(self):
        # story_threads_intro is a single-chunk section (fewer claims than the
        # dense-section threshold), so the retry arithmetic below is the whole
        # section's: 1 try + 2 retries. Dense sections are covered by
        # ChunkedAuthoringTests.
        section = next(s for s in FEDERAL["sections"]
                       if s["key"] == "story_threads_intro")
        writer = FixtureWriter("The projection is 150 government-aligned seats.")
        record = ar.author_section(FEDERAL, section, self.index,
                                   self.allowed, writer, "en", retries=2)
        self.assertEqual(record["render"], "degraded")
        self.assertEqual(record["attempts"], 3)          # 1 try + 2 retries
        self.assertEqual(len(writer.calls), 3)
        self.assertEqual([v["token"] for v in record["violations"]], ["150"])
        self.assertEqual(record["body"],
                         ar.render_fallback(FEDERAL, section, self.index, "en"))

    def test_a_section_that_passes_is_never_retried(self):
        writer = FixtureWriter(lambda key: ar.render_fallback(
            FEDERAL, next(s for s in FEDERAL["sections"] if s["key"] == key),
            self.index, "en"))
        record = ar.author_section(FEDERAL, FEDERAL["sections"][0], self.index,
                                   self.allowed, writer, "en", retries=2)
        self.assertEqual(record["render"], "authored")
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["violations"], [])

    def test_a_transport_error_degrades_instead_of_crashing(self):
        def boom(prompt, system, section=None):
            raise ar.AuthoringError("upstream 503")

        record = ar.author_section(FEDERAL, FEDERAL["sections"][0], self.index,
                                   self.allowed, type("W", (), {"write": staticmethod(boom)})(),
                                   "en", retries=2)
        self.assertEqual(record["render"], "degraded")
        self.assertIn("upstream 503", record["error"])
        self.assertTrue(record["body"])

    def test_claim_id_lookup_returns_the_pack_value(self):
        self.assertEqual(self.index["fed:P50:139"]["value"], 139)
        self.assertEqual(self.index["fed:bloc:BN:34"]["value"], 34)
        self.assertEqual(rp.claim_index(KEDAH)["dun:mc_P50:139"]["value"], 139)


class ManifestAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.index = rp.claim_index(FEDERAL)

    def _section(self, key):
        return next(s for s in FEDERAL["sections"] if s["key"] == key)

    def test_manifest_records_the_writer_model_and_binds_pack_and_config(self):
        out_ai, work, pack_root = paths("manifest")
        config = ar.load_writer_config()
        pack_file = str(Path(pack_root) / "pack.json")
        writer = FixtureWriter(lambda key: ar.render_fallback(
            FEDERAL, self._section(key), self.index, "en"))
        markdown, records = ar.author_document(
            FEDERAL, writer=writer, lang="en", writer_label="fixture",
            pack_path=pack_file)
        _, _, manifest_file, manifest = ar.write_outputs(
            FEDERAL, markdown, records, config, False, out_root=out_ai,
            work_root=work, writer_label="fixture", pack_path=pack_file)
        self.assertEqual(manifest["writer"]["model"], config["model"])
        self.assertEqual(manifest["writer"]["provider"], config["provider"])
        self.assertEqual(manifest["mode"], "live")
        self.assertEqual(manifest["writer_config_hash"], ar.writer_config_hash(config))
        self.assertEqual(manifest["pack"]["hash"], FEDERAL["pack_hash"])
        self.assertEqual(manifest["verify"]["authored"], len(records))
        self.assertEqual(manifest["degraded_sections"], [])
        on_disk = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["writer"], manifest["writer"])
        self.assertEqual(on_disk["sections"][0]["render"], "authored")
        self.assertEqual(on_disk["sections"][0]["attempts"], 1)

    def test_writer_config_hash_binds_the_model_choice(self):
        config = ar.load_writer_config()
        other = dict(config)
        other["model"] = "some/other-model"
        self.assertNotEqual(ar.writer_config_hash(config), ar.writer_config_hash(other))

    def test_writer_defaults_and_file_override(self):
        defaults = ar.load_writer_config(str(Path(TMP) / "absent-config.json"))
        self.assertEqual(defaults["provider"], "nous")
        self.assertEqual(defaults["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(defaults["temperature"], 0.7)
        self.assertEqual(defaults["base_url"], ar.PROVIDER_BASE_URLS["nous"])
        custom = Path(TMP) / "authoring-config.json"
        custom.write_text(json.dumps({
            "provider": "deepseek", "model": "deepseek-v4-pro",
            "temperature": 0.0, "max_tokens": 2048}), encoding="utf-8")
        config = ar.load_writer_config(str(custom))
        self.assertEqual(config["model"], "deepseek-v4-pro")
        self.assertEqual(config["temperature"], 0.0)
        self.assertEqual(config["base_url"], ar.PROVIDER_BASE_URLS["deepseek"])
        self.assertNotEqual(ar.writer_config_hash(config),
                            ar.writer_config_hash(defaults))

    def test_the_shipped_config_is_valid_and_names_a_swappable_writer(self):
        self.assertTrue(Path(ar.WRITER_CONFIG).is_file())
        config = ar.load_writer_config()
        self.assertTrue(config["provider"] and config["model"])
        self.assertIn("api_key_env", config)

    def test_a_live_writer_without_a_key_fails_before_any_request(self):
        config = ar.load_writer_config(str(Path(TMP) / "absent-config.json"))
        config["api_key_env"] = "GE16_TEST_KEY_THAT_IS_ABSENT"
        self.assertIsNone(os.environ.get("GE16_TEST_KEY_THAT_IS_ABSENT"))
        with self.assertRaises(ar.AuthoringError):
            ar.LiveWriter(config).write("prompt", "system", section="intro_context")


class PromptAndCliTests(unittest.TestCase):
    def setUp(self):
        self.index = rp.claim_index(FEDERAL)

    def test_prompt_carries_only_that_sections_facts(self):
        section = next(s for s in FEDERAL["sections"] if s["key"] == "watch_list")
        prompt = ar.build_prompt(FEDERAL, section, self.index, "en")
        for claim_id in section["claim_ids"]:
            self.assertIn(claim_id, prompt)
        self.assertNotIn("fed:macro:gdp_yoy:6.0", prompt)
        self.assertIn(section["brief_en"], prompt)
        self.assertIn("copy the `value` column verbatim", prompt)

    def test_malay_prompt_is_written_in_malay(self):
        section = FEDERAL["sections"][0]
        prompt = ar.build_prompt(FEDERAL, section, self.index, "ms")
        self.assertIn("SENARAI FAKTA", prompt)
        self.assertIn("RINGKASAN", prompt)
        self.assertIn(section["brief_ms"], prompt)
        self.assertIn("VERBATIM", ar.system_prompt("ms"))

    def test_section_filter_authors_only_the_requested_sections(self):
        out_ai, work, pack_root = paths("filter")
        result = ar.run_authoring(lang="en", offline=True, pack=FEDERAL,
                                  sections=["intro_context", "closing"],
                                  out_root=out_ai, work_root=work,
                                  pack_root=pack_root)
        self.assertEqual([r["key"] for r in result["records"]],
                         ["intro_context", "closing"])
        self.assertIn("## 1. ", result["markdown"])
        self.assertIn("## 9. ", result["markdown"])
        self.assertNotIn("## 5. ", result["markdown"])

    def test_cli_offline_run_writes_the_state_edition(self):
        out_ai, work, pack_root = paths("cli")
        code = ar.main(["--lang", "en", "--state", "Kedah", "--offline",
                        "--out-root", out_ai, "--work-root", work,
                        "--pack-root", pack_root])
        self.assertEqual(code, 0)
        edition = Path(out_ai) / "states" / "DUN Kedah" / "GE16_Kedah_Report.md"
        self.assertTrue(edition.is_file())
        self.assertTrue(Path(out_ai, "states", "DUN Kedah", "archive",
                             "GE16-{0}".format(KEDAH["as_of"]),
                             "GE16_Kedah_Report.md").is_file())

    def test_cli_offline_never_reaches_a_writer(self):
        original = ar.LiveWriter
        ar.LiveWriter = ForbiddenWriter
        try:
            out_ai, work, pack_root = paths("cli-offline")
            code = ar.main(["--lang", "ms", "--offline", "--out-root", out_ai,
                            "--work-root", work, "--pack-root", pack_root,
                            "--no-emit-pack"])
        finally:
            ar.LiveWriter = original
        self.assertEqual(code, 0)

    def test_cli_list_sections_reads_an_existing_pack(self):
        pack_file = rp.write_pack(FEDERAL, out_path=str(Path(TMP) / "pack-list.json"))
        self.assertEqual(ar.main(["--list-sections", "--pack", pack_file]), 0)


#: Words that describe how the report is MADE. A reader of the report is a reader
#: of Malaysian politics: none of these may reach the page, in either language.
MACHINERY_WORDS = (
    "ledger", "claim", "pack", "window", "vintage", "degraded", "retry", "retries",
    "fallback", "deterministic", "authoritative", "citation", "manifest", "schema",
    "deck", "triage", "anchor",
)
MACHINERY_WORDS_MS = (
    "lejar", "dakwaan", "pek", "tetingkap", "vintaj", "dinyahgred", "cubaan semula",
    "sandaran", "deterministik", "berwibawa", "sitasi", "manifes", "skema", "triaj",
    "sauh",
)


def _paths(tag):
    base = Path(TMP) / ("focus-" + tag)
    return (str(base / "ai"), str(base / "work"), str(base / "pack"))


#: The registry vocabulary a reader must never meet on the page (F3): the
#: seat-type tokens, the ALL-CAPS registry names and the raw field names that
#: used to reach the reader as ``key: value``.
FORBIDDEN_PAGE_TOKENS = (
    "pn_core:", "mixed_malay:", "true_mixed:", "non_malay:", "east_malaysia:",
    "MACRO:", "MAKRO:", "MACRO reading", "Seats typed", "seat_type:", "seat_type_count",
    "gdp_yoy", "cpi_yoy", "event_shocks",
)

#: A key prefix in the reader's notes: ``^40 pn_core: 80.0 % ...``.
NOTES_KEY_PREFIX_RE = re.compile(r"^\^\d+ `?[A-Z][A-Za-z_]+:")
#: The label segment of a notes entry, with a raw key prefix: ``pn_core: 80 %``.
LABEL_KEY_PREFIX_RE = re.compile(r"^`?[A-Za-z][A-Za-z0-9_]*:")
_CITATION_RE = re.compile(r"\[[^\]\n]*\]")


def spoken(text):
    """A rendered body as the reader meets it: citation markers taken out."""
    return _CITATION_RE.sub("", text or "")


class StubWriter:
    """A writer for tests: a label, optional canned text, optional failure."""

    def __init__(self, label, text="", error=None):
        self.label = label
        self.text = text
        self.error = error
        self.calls = []

    def write(self, prompt, system, section=None):
        self.calls.append({"section": section, "prompt": prompt})
        if self.error is not None:
            raise self.error
        return self.text


class ReaderFocusTests(unittest.TestCase):
    """The published editions are written for readers of the subject matter.

    Every offline edition (federal + state, EN + MS) is rendered and scanned: the
    harness may hold its own bookkeeping in the manifest, but nothing about how
    the report was assembled may appear in the report.
    """

    @classmethod
    def setUpClass(cls):
        cls.editions = {}
        for tag, kwargs in (("fed-en", {"lang": "en"}),
                            ("fed-ms", {"lang": "ms"}),
                            ("kedah-en", {"lang": "en", "state": "Kedah"}),
                            ("kedah-ms", {"lang": "ms", "state": "Kedah"})):
            out_ai, work, pack_root = _paths(tag)
            cls.editions[tag] = ar.run_authoring(
                offline=True, out_root=out_ai, work_root=work, pack_root=pack_root,
                **kwargs)["markdown"]

    def test_no_edition_names_the_machinery(self):
        """The PROSE is machinery-free; the title and footer state the mode.

        Two harness lines are exempt from the prose scan — the H1 title and the
        footer — because a reader must be told whether the edition was written by
        a model at all (F1). Both are asserted separately below and by
        ``test_header_is_a_title_and_the_footer_is_the_only_harness_line``.
        """
        for tag, markdown in self.editions.items():
            words = MACHINERY_WORDS_MS if tag.endswith("ms") else MACHINERY_WORDS
            lines = [line for line in markdown.splitlines() if line.strip()]
            prose = "\n".join(lines[2:-1])
            for word in words:
                self.assertIsNone(
                    re.search(r"\b{0}\b".format(re.escape(word)), prose, re.IGNORECASE),
                    "{0}: '{1}' reached the prose".format(tag, word))

    def test_edition_titles_state_the_mode(self):
        """An edition no writer authored is titled as a model report, in both
        languages, and never as an AI edition; the state edition says so too."""
        for tag, markdown in self.editions.items():
            lines = [line for line in markdown.splitlines() if line.strip()]
            self.assertTrue(lines[0].startswith("# GE16 —"), lines[0])
            self.assertNotIn("AI-Authored", lines[0])
            self.assertNotIn("Ditulis AI", lines[0])
            self.assertIn("deterministic render" if not tag.endswith("ms")
                          else "paparan deterministik", lines[0])
            self.assertTrue(ar.is_edition_footer(lines[-1]), lines[-1])
            self.assertIn(lines[-1], ar.OFFLINE_FOOTERS)

    def test_no_claim_id_survives_into_the_page(self):
        for tag, markdown in self.editions.items():
            self.assertIsNone(
                re.search(r"\[(fed|dun|state):[^\]\n]*\]", markdown),
                "{0}: a raw claim id reached the page".format(tag))
            self.assertNotIn("work/events", markdown)
            self.assertNotIn("authoring_config", markdown)

    #: The exact vocabulary the reader-focus review found on the published page,
    #: plus the second sweep's find: the pack's own value KIND printed as a unit
    #: (" text — ") and a raw enum value ("high_risk") on the page at all.
    REVIEWED_TOKENS = (
        "RENDER=degraded", "Data vintage", "Reference pack", "Fact deck",
        "Dek fakta", "[fed:", "[dun:", "ledger", "Degraded:",
        "Rujukan pek rujukan", "Reference-pack", "story ledger",
        "high_risk", "super_marginal", " text —", " date —", " seed —",
    )

    #: The same residue, as the shapes the second review grepped for.
    MACHINE_RESIDUE_RE = re.compile(r"\b(high_risk|super_marginal)\b| text —| date —| seed —")

    def test_no_produced_note_reads_a_machine_value_or_unit(self):
        """F3: a note lists a figure, its unit and its label.

        The pack's own value KIND (``text``, ``date``, ``seed``) is not a unit of
        measure, and an enum value (``high_risk``) is not prose — neither may
        appear in the reader's Notes, in any of the four editions.
        """
        for tag, markdown in self.editions.items():
            notes = markdown.split("## Notes / Catatan", 1)[1]
            entries = [line for line in notes.splitlines() if line.startswith("^")]
            self.assertTrue(entries, "{0}: the notes list carries the figures".format(tag))
            for entry in entries:
                self.assertIsNone(self.MACHINE_RESIDUE_RE.search(entry),
                                  "{0}: machine vocabulary in a note: {1}".format(tag, entry))

    def test_produced_editions_carry_none_of_the_reviewed_tokens(self):
        for tag, markdown in self.editions.items():
            for token in self.REVIEWED_TOKENS:
                self.assertNotIn(token, markdown,
                                 "{0}: '{1}' reached the page".format(tag, token))

    def test_produced_editions_carry_no_registry_token(self):
        """F3: the raw key vocabulary never reaches the page, either language."""
        for tag, markdown in self.editions.items():
            for token in FORBIDDEN_PAGE_TOKENS:
                self.assertNotIn(token, markdown,
                                 "{0}: registry token '{1}' reached the page".format(tag, token))

    def test_the_files_on_disk_carry_no_registry_token(self):
        """The same grep over every file the run actually wrote."""
        for tag in ("fed-en", "fed-ms", "kedah-en", "kedah-ms"):
            out_ai, _, _ = _paths(tag)
            written = sorted(Path(out_ai).rglob("*.md"))
            self.assertTrue(written, tag)
            for path in written:
                text = path.read_text(encoding="utf-8")
                for token in FORBIDDEN_PAGE_TOKENS:
                    self.assertNotIn(token, text,
                                     "{0}: '{1}' in {2}".format(tag, token, path))

    def test_every_notes_line_reads_as_a_figure_then_words(self):
        """F3: ``^n <value> <unit> — <words>`` — no key prefix, no registry name.

        A key can only reach a notes line in its LABEL (the text after the em
        dash) or, in the prefix-first form the review found, before the value: the
        packet's ``^\\^\\d+ \\`?[A-Z][A-Za-z_]+:`` guard is asserted for that
        second form and is proven non-vacuous by
        ``LabelMappingTests.test_the_notes_guard_would_catch_a_raw_key``. The
        label is where today's leaks lived, so it is checked for a key prefix, for
        a registry token and for any snake_case field name. A quoted pack value
        may legitimately begin with a word and a colon (``CGSI: Budget 2027 ...``)
        — that is content, not a key.
        """
        for tag, markdown in self.editions.items():
            notes = markdown.split("## Notes / Catatan", 1)[1]
            entries = [line for line in notes.splitlines() if line.startswith("^")]
            self.assertTrue(entries, "{0}: the notes list carries the figures".format(tag))
            for entry in entries:
                self.assertRegex(entry, r"^\^\d+ \S.*— \S")
                match = NOTES_KEY_PREFIX_RE.match(entry)
                if match:
                    # A quoted headline may legitimately begin "CGSI: ..."; a key
                    # prefix never looks like that. Only registry vocabulary (or a
                    # snake_case field name) counts as a leak here.
                    word = re.match(r"^\^\d+ `?([A-Za-z][A-Za-z0-9_]*):", entry).group(1)
                    self.assertFalse(
                        word in ar.LABEL_TOKENS or "_" in word,
                        "{0}: a key prefix reached the reader's notes: {1}".format(tag, entry))
                label = entry.rsplit(" — ", 1)[-1]
                self.assertIsNone(
                    NOTES_KEY_PREFIX_RE.match("^1 {0}".format(label)),
                    "{0}: the notes label carries a key prefix: {1}".format(tag, entry))
                self.assertIsNone(
                    LABEL_KEY_PREFIX_RE.match(label),
                    "{0}: the notes label carries a key prefix: {1}".format(tag, entry))
                self.assertIsNone(
                    re.search(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b", entry),
                    "{0}: an ALL-CAPS registry name reached the notes: {1}".format(tag, entry))
                self.assertNotIn("_", label,
                                 "{0}: a field name reached the notes: {1}".format(tag, entry))
                for token in FORBIDDEN_PAGE_TOKENS:
                    self.assertNotIn(token, entry,
                                     "{0}: '{1}' in a note: {2}".format(tag, token, entry))

    def test_the_files_on_disk_carry_none_of_the_reviewed_tokens(self):
        """The same grep over what the run actually wrote, not the returned text."""
        for tag in ("fed-en", "fed-ms", "kedah-en", "kedah-ms"):
            out_ai, _, _ = _paths(tag)
            written = sorted(Path(out_ai).rglob("*.md"))
            self.assertTrue(written, tag)
            for path in written:
                text = path.read_text(encoding="utf-8")
                self.assertGreater(len(text), 500, path)
                for token in self.REVIEWED_TOKENS:
                    self.assertNotIn(token, text,
                                     "{0}: '{1}' in {2}".format(tag, token, path))

    def test_header_is_a_title_and_the_footer_is_the_only_harness_line(self):
        for tag, markdown in self.editions.items():
            lines = [line for line in markdown.splitlines() if line.strip()]
            self.assertTrue(lines[0].startswith("# GE16 —"), lines[0])
            self.assertEqual(lines[1], "---")
            self.assertRegex(lines[2], r"^## \d+\. \S")
            self.assertNotIn("**Writer:**", markdown)
            self.assertNotIn("**Language:**", markdown)
            self.assertNotIn("**Reference pack:**", markdown)
            self.assertNotIn("Sections authored", markdown)
            footer = lines[-1]
            self.assertTrue(ar.is_edition_footer(footer), footer)
            self.assertIn("## Notes / Catatan", markdown)

    def test_every_figure_in_the_notes_is_described_in_words(self):
        markdown = self.editions["fed-en"]
        notes = markdown.split("## Notes / Catatan", 1)[1]
        entries = [line for line in notes.splitlines() if line.startswith("^")]
        self.assertTrue(entries, "the notes list carries the figures behind the prose")
        for entry in entries:
            self.assertRegex(entry, r"^\^\d+ \S.*— \S")
            self.assertNotIn("`", entry, "no machine identifier in a reader's notes")

    def test_both_system_prompts_state_the_reader_rule(self):
        cases = (("en", "Write for a reader of political analysis",
                  ("ledger", "vintage", "degraded", "manifest")),
                 ("ms", "Tulis untuk pembaca analisis politik",
                  ("lejar", "vintaj", "dinyahgred", "manifes")))
        for lang, phrase, forbidden in cases:
            prompt = ar.system_prompt(lang)
            self.assertIn(phrase, prompt)
            for word in forbidden:
                self.assertIn(word, prompt, "the rule names the words it forbids")


class WriterLanguageTests(unittest.TestCase):
    """EN and MS are separate writer passes, chosen by the shipped config.

    The split is a property of the config file the step actually runs with, not
    of a hand-swapped flat config: the two languages must resolve to different
    writers and hash into different manifests.
    """

    def test_shipped_config_selects_a_different_writer_per_language(self):
        en = ar.load_writer_config(ar.WRITER_CONFIG, "en")
        ms = ar.load_writer_config(ar.WRITER_CONFIG, "ms")
        self.assertEqual(en["model"], "deepseek-chat",
                         "the shipped EN primary is deepseek-chat")
        self.assertEqual(en["base_url"], "https://api.deepseek.com")
        self.assertEqual(en["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(ms["model"], "ilmu-v3.1",
                         "the shipped MS primary is ilmu-v3.1")
        self.assertNotEqual(en["model"], ms["model"])
        self.assertNotEqual(en["base_url"], ms["base_url"])
        self.assertNotEqual(ar.writer_config_hash(en), ar.writer_config_hash(ms),
                            "each language pass must hash its own writer")

    def test_language_block_overrides_only_the_keys_it_names(self):
        path = Path(TMP) / "authoring_config-languages.json"
        path.write_text(json.dumps({
            "provider": "nous", "model": "flat-model", "max_tokens": 4096,
            "languages": {"en": {"model": "glm-5.3-flash",
                                 "base_url": "https://opencode.ai/zen/go/v1",
                                 "api_key_env": "OPENCODE_GO_API_KEY"}},
        }), encoding="utf-8")
        en = ar.load_writer_config(str(path), "en")
        ms = ar.load_writer_config(str(path), "ms")
        self.assertEqual(en["model"], "glm-5.3-flash")
        self.assertEqual(en["api_key_env"], "OPENCODE_GO_API_KEY")
        self.assertEqual(en["max_tokens"], 4096,
                         "keys the language block does not name keep the flat value")
        self.assertEqual(ms["model"], "flat-model",
                         "a language with no block falls back to the flat config")
        self.assertNotEqual(ar.writer_config_hash(en), ar.writer_config_hash(ms))

    def test_a_flat_config_still_writes_every_language(self):
        path = Path(TMP) / "authoring_config-flat.json"
        path.write_text(json.dumps({"provider": "nous", "model": "only-model"}),
                        encoding="utf-8")
        for lang in ("en", "ms", None):
            self.assertEqual(ar.load_writer_config(str(path), lang)["model"],
                             "only-model")


class FailoverTests(unittest.TestCase):
    """F5: one section, several writers — the next one gets the facts.

    The chain comes from the shipped config (``languages.<lang>.fallbacks``) and
    is tried per SECTION, so a writer that cannot deliver one section's facts does
    not decide the fate of the others. The manifest names the writer that
    actually wrote each section.
    """

    def setUp(self):
        self.index = rp.claim_index(FEDERAL)
        self.allowed = rp.allowed_tokens(FEDERAL)

    def _section(self, key):
        return next(s for s in FEDERAL["sections"] if s["key"] == key)

    def test_the_shipped_en_chain_is_primaried_by_deepseek_chat(self):
        """The owner's writer split: deepseek-chat writes the EN edition first.

        glm-5.3-flash stays in the EN config as the failover — it still verifies
        when it answers, it is just too slow at real section prompts to lead.
        """
        en = ar.load_writer_chain(ar.WRITER_CONFIG, "en")
        self.assertEqual(en[0]["model"], "deepseek-chat",
                         "deepseek-chat is the EN PRIMARY, not a fallback")
        self.assertEqual(en[0]["base_url"], "https://api.deepseek.com")
        self.assertEqual(en[0]["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(ar.load_writer_config(ar.WRITER_CONFIG, "en")["model"],
                         "deepseek-chat",
                         "the EN pass's config (and so its manifest hash) is deepseek-chat")
        self.assertEqual([item["model"] for item in en[1:]], ["glm-5.3-flash"],
                         "glm-5.3-flash is the EN failover, tried when deepseek cannot deliver")
        ms = ar.load_writer_chain(ar.WRITER_CONFIG, "ms")
        self.assertEqual(ms[0]["model"], "ilmu-v3.1", "the MS chain is unchanged")
        self.assertEqual([item["model"] for item in ms[1:]],
                         ["ilmu-mini-v3.3", "deepseek-chat"])

    def test_a_dead_writer_hands_the_section_to_the_next_one(self):
        section = self._section("story_threads_intro")
        broken = StubWriter("stub/broken", error=ar.AuthoringError("upstream 503"))
        good = StubWriter("stub/good",
                          text=ar.render_fallback(FEDERAL, section, self.index, "en"))
        record = ar.author_section(FEDERAL, section, self.index, self.allowed, None,
                                   "en", retries=2, writers=[broken, good])
        self.assertEqual(record["render"], "authored")
        self.assertEqual(record["writer"], "stub/good")
        self.assertEqual(record["writers_tried"], ["stub/broken", "stub/good"])
        self.assertEqual(len(broken.calls), 1, "a dead writer is not retried")
        self.assertEqual(len(good.calls), 1, "and the writer that works is not retried")

    def test_a_writer_that_keeps_failing_verification_hands_over(self):
        section = self._section("story_threads_intro")
        mutant = StubWriter("stub/mutant",
                            text="The projection is 150 government-aligned seats.")
        good = StubWriter("stub/good",
                          text=ar.render_fallback(FEDERAL, section, self.index, "en"))
        record = ar.author_section(FEDERAL, section, self.index, self.allowed, None,
                                   "en", retries=2, writers=[mutant, good])
        self.assertEqual(record["render"], "authored")
        self.assertEqual(record["writer"], "stub/good")
        self.assertEqual(len(mutant.calls), 3, "1 try + 2 retries before failing over")
        self.assertEqual(record["violations"], [], "the published text is clean")

    def test_every_writer_failing_leaves_the_deterministic_text(self):
        section = self._section("story_threads_intro")
        first = StubWriter("stub/1", text="The projection is 150 government-aligned seats.")
        second = StubWriter("stub/2", text="Only 199 seats are in play.")
        record = ar.author_section(FEDERAL, section, self.index, self.allowed, None,
                                   "en", retries=1, writers=[first, second])
        self.assertEqual(record["render"], "degraded")
        self.assertIsNone(record["writer"])
        self.assertEqual(record["writers_tried"], ["stub/1", "stub/2"])
        self.assertEqual(record["body"],
                         ar.render_fallback(FEDERAL, section, self.index, "en"))

    def test_the_shipped_config_declares_a_chain_per_language(self):
        en = ar.load_writer_chain(ar.WRITER_CONFIG, "en")
        ms = ar.load_writer_chain(ar.WRITER_CONFIG, "ms")
        self.assertEqual([item["model"] for item in en],
                         ["deepseek-chat", "glm-5.3-flash"])
        self.assertEqual(en[0]["base_url"], "https://api.deepseek.com")
        self.assertEqual(en[0]["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(en[1]["base_url"], "https://opencode.ai/zen/go/v1")
        self.assertEqual(en[1]["api_key_env"], "OPENCODE_GO_API_KEY")
        self.assertEqual([item["model"] for item in ms],
                         ["ilmu-v3.1", "ilmu-mini-v3.3", "deepseek-chat"])
        self.assertEqual(ms[0]["base_url"], "https://api.ilmu.ai/v1")
        self.assertEqual(ms[1]["base_url"], "https://api.ilmu.ai/v1")
        self.assertNotEqual(ar.writer_chain_hash(en), ar.writer_chain_hash(ms))
        self.assertEqual(ar.writer_chain_hash(en), ar.writer_chain_hash(
            ar.load_writer_chain(ar.WRITER_CONFIG, "en")), "the chain hash is stable")

    def test_a_single_writer_config_is_a_chain_of_one(self):
        path = Path(TMP) / "authoring_config-single.json"
        path.write_text(json.dumps({"provider": "nous", "model": "only-model"}),
                        encoding="utf-8")
        self.assertEqual([item["model"] for item in ar.load_writer_chain(str(path), "en")],
                         ["only-model"])

    def test_a_fallback_block_inherits_the_keys_it_does_not_name(self):
        path = Path(TMP) / "authoring_config-chain.json"
        path.write_text(json.dumps({
            "provider": "custom", "base_url": "https://primary.example/v1",
            "api_key_env": "PRIMARY_KEY", "model": "primary", "max_tokens": 4096,
            "languages": {"en": {
                "model": "en-primary",
                "fallbacks": [
                    {"model": "en-second"},
                    {"model": "en-third", "provider": "deepseek",
                     "api_key_env": "DEEPSEEK_API_KEY",
                     "base_url": "https://api.deepseek.com", "max_tokens": 8192},
                ]}},
        }), encoding="utf-8")
        chain = ar.load_writer_chain(str(path), "en")
        self.assertEqual([item["model"] for item in chain],
                         ["en-primary", "en-second", "en-third"])
        self.assertEqual(chain[1]["base_url"], "https://primary.example/v1",
                         "an unnamed key keeps the primary's value")
        self.assertEqual(chain[1]["api_key_env"], "PRIMARY_KEY")
        self.assertEqual(chain[1]["max_tokens"], 4096)
        self.assertEqual(chain[2]["base_url"], "https://api.deepseek.com")
        self.assertEqual(chain[2]["max_tokens"], 8192)

    def test_a_bad_fallbacks_block_is_an_authoring_error(self):
        path = Path(TMP) / "authoring_config-bad-chain.json"
        path.write_text(json.dumps({"model": "primary", "fallbacks": "deepseek-chat"}),
                        encoding="utf-8")
        with self.assertRaises(ar.AuthoringError):
            ar.load_writer_chain(str(path), "en")

    def test_the_manifest_names_the_writer_of_every_section(self):
        out, work, pack_root = paths("failover-manifest")
        original = ar.LiveWriter

        class FailoverWriter(object):
            """The EN primary (deepseek-chat) is unreachable; glm-5.3-flash answers."""

            def __init__(self, config):
                self.config = config
                self.label = "{0}/{1}".format(config.get("provider"), config.get("model"))

            def write(self, prompt, system, section=None):
                if self.config.get("model") != "glm-5.3-flash":
                    raise ar.AuthoringError("upstream 503")
                key = section
                target = next(s for s in FEDERAL["sections"] if s["key"] == key)
                return ar.render_fallback(FEDERAL, target, rp.claim_index(FEDERAL), "en")

        ar.LiveWriter = FailoverWriter
        try:
            result = ar.run_authoring(lang="en", pack=FEDERAL, out_root=out,
                                      work_root=work, pack_root=pack_root)
        finally:
            ar.LiveWriter = original
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual([item["model"] for item in manifest["writer_chain"]],
                         ["deepseek-chat", "glm-5.3-flash"])
        self.assertEqual(manifest["writer"]["model"], "deepseek-chat",
                         "the pass's primary is still recorded as the writer")
        self.assertEqual(manifest["verify"]["authored"], len(FEDERAL["sections"]))
        for section in manifest["sections"]:
            self.assertEqual(section["writer"], "custom/glm-5.3-flash")
            self.assertEqual(section["writers_tried"],
                             ["deepseek/deepseek-chat", "custom/glm-5.3-flash"])
        self.assertEqual(manifest["edition_mode"], "ai-authored")
        self.assertIn("AI-Authored", result["markdown"])


class ChunkedAuthoringTests(unittest.TestCase):
    """F2: a dense section is written in chunks and verified chunk by chunk."""

    def setUp(self):
        self.index = rp.claim_index(FEDERAL)
        self.allowed = rp.allowed_tokens(FEDERAL)
        self.section = next(s for s in FEDERAL["sections"]
                            if s["key"] == "story_threads_items")

    def test_a_dense_section_splits_into_chunks_of_at_most_the_limit(self):
        chunks = ar.section_chunks(self.section, self.index)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= ar.CHUNK_CLAIM_LIMIT for chunk in chunks))
        self.assertTrue(all(chunk for chunk in chunks))
        flat = [cid for chunk in chunks for cid in chunk]
        self.assertEqual(flat, list(self.section["claim_ids"]),
                         "every claim is written exactly once, in pack order")

    def test_group_families_stay_together_inside_a_chunk(self):
        chunks = ar.section_chunks(self.section, self.index)
        for chunk in chunks:
            for cid in chunk:
                claim = self.index[cid]
                family = claim.get("group") or claim.get("family")
                if not family:
                    continue
                # a family that fits in one chunk is never split across two
                mates = [c for c in self.section["claim_ids"]
                         if (self.index[c].get("group") or self.index[c].get("family")) == family]
                if len(mates) <= ar.CHUNK_CLAIM_LIMIT:
                    self.assertTrue(set(mates) <= set(chunk),
                                    "no chunk of the section holds a group whole")
                    break

    def test_a_small_section_is_one_chunk(self):
        section = next(s for s in FEDERAL["sections"]
                       if s["key"] == "story_threads_intro")
        self.assertEqual(ar.section_chunks(section, self.index),
                         [list(section["claim_ids"])])

    def test_a_chunk_prompt_carries_only_that_chunks_facts(self):
        chunks = ar.section_chunks(self.section, self.index)
        prompt = ar.build_prompt(FEDERAL, self.section, self.index, "en",
                                 claim_ids=chunks[0], part=1, parts=len(chunks))
        for cid in chunks[0]:
            self.assertIn("  - {0} |".format(cid), prompt)
        for cid in chunks[1]:
            self.assertNotIn("  - {0} |".format(cid), prompt)
        self.assertIn("part 1 of {0}".format(len(chunks)), prompt)

    def test_a_chunk_fallback_covers_only_that_chunks_facts(self):
        chunks = ar.section_chunks(self.section, self.index)
        body = ar.render_fallback(FEDERAL, self.section, self.index, "en",
                                  claim_ids=chunks[0])
        self.assertTrue(body)
        self.assertEqual(ar.verify_text(body, self.allowed, self.index), [],
                         "the chunk's deterministic text passes the same verifier")

    def test_one_bad_chunk_degrades_alone(self):
        chunks = ar.section_chunks(self.section, self.index)
        bad = set(chunks[1])
        mutant = "The projection is 150 government-aligned seats."
        good = ar.render_fallback(FEDERAL, self.section, self.index, "en")

        class ChunkWriter(object):
            label = "stub/chunked"

            def __init__(self):
                self.calls = []

            def write(self, prompt, system, section=None):
                self.calls.append(prompt)
                for cid in bad:
                    if "  - {0} |".format(cid) in prompt:
                        return mutant
                return good

        writer = ChunkWriter()
        record = ar.author_section(FEDERAL, self.section, self.index, self.allowed,
                                   writer, "en", retries=1)
        self.assertEqual(record["render"], "chunked-partial",
                         "the section is not degraded: only one chunk is")
        self.assertEqual(len(record["chunks"]), len(chunks))
        renders = [item["render"] for item in record["chunks"]]
        self.assertEqual(renders.count("authored"), len(chunks) - 1)
        self.assertEqual(renders.index("degraded"), 1, "the second chunk is the one that failed")
        self.assertNotIn(mutant, record["body"])
        deterministic = ar.render_fallback(FEDERAL, self.section, self.index, "en",
                                           claim_ids=chunks[1])
        self.assertIn(deterministic, record["body"])
        self.assertEqual(ar.verify_text(record["body"], self.allowed, self.index), [])
        self.assertGreater(len(writer.calls), len(chunks),
                           "the bad chunk was retried before it degraded")

    def test_a_dense_section_with_every_chunk_failing_is_degraded(self):
        chunks = ar.section_chunks(self.section, self.index)
        writer = StubWriter("stub/mutant",
                            text="The projection is 150 government-aligned seats.")
        record = ar.author_section(FEDERAL, self.section, self.index, self.allowed,
                                   writer, "en", retries=1)
        self.assertEqual(record["render"], "degraded")
        self.assertEqual(len(record["chunks"]), len(chunks))
        self.assertTrue(all(item["render"] == "degraded" for item in record["chunks"]))
        self.assertNotIn("150", record["body"])

    def test_the_dense_sections_are_the_ones_over_the_threshold(self):
        dense = {section["key"] for section in FEDERAL["sections"]
                 if len(section["claim_ids"]) > ar.DENSE_SECTION_CLAIMS}
        self.assertTrue({"electorate", "electorate_dynamics", "story_threads_items",
                         "scenario_narrative"} <= dense, dense)
        for section in FEDERAL["sections"]:
            chunks = ar.section_chunks(section, self.index)
            if len(section["claim_ids"]) <= ar.DENSE_SECTION_CLAIMS:
                self.assertEqual(len(chunks), 1, section["key"])


class PublishModeTests(unittest.TestCase):
    """F1: the title/footer follow the mode; nothing verifies -> nothing written."""

    def _run_with_mutant_writer(self, lang, pack, tag):
        out, work, pack_root = paths(tag)
        original = ar.LiveWriter

        class MutantWriter(object):
            def __init__(self, config):
                self.config = config
                self.label = "stub/mutant"

            def write(self, prompt, system, section=None):
                return "The projection is 150 government-aligned seats."

        ar.LiveWriter = MutantWriter
        try:
            return ar.run_authoring(lang=lang, pack=pack, out_root=out, work_root=work,
                                    pack_root=pack_root), out
        finally:
            ar.LiveWriter = original

    def test_a_live_pass_that_authors_nothing_publishes_nothing(self):
        result, out = self._run_with_mutant_writer("en", FEDERAL, "no-publish")
        self.assertFalse(result["published"])
        self.assertIsNone(result["edition"])
        self.assertIsNone(result["archive"])
        self.assertEqual(sorted(Path(out).rglob("*.md")), [],
                         "no edition may be written when no section verified")
        manifest = result["manifest_data"]
        self.assertFalse(manifest["published"])
        self.assertEqual(manifest["edition_mode"], "not-published")
        self.assertEqual(manifest["mode"], "offline",
                         "a pass with no authored section is not an AI edition")
        self.assertEqual(manifest["verify"]["ai_assisted"], 0)
        self.assertIn("no section passed verification", manifest["skipped_reason"])
        self.assertIn("deterministic render", manifest["title"])
        self.assertNotIn("AI-Authored", manifest["title"])
        self.assertTrue(Path(result["manifest"]).is_file(), "the failure is recorded")
        raw = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(raw["published"], False)

    def test_a_previous_edition_is_never_overwritten_by_a_fallback(self):
        result, out = self._run_with_mutant_writer("en", KEDAH, "keep-prior")
        edition, archive = ar.output_paths(KEDAH, out)
        Path(edition).parent.mkdir(parents=True, exist_ok=True)
        Path(edition).write_text("# GE16 — Kedah (AI-Authored Edition)\n\nGOOD PRIOR\n",
                                 encoding="utf-8")
        Path(archive).parent.mkdir(parents=True, exist_ok=True)
        Path(archive).write_text("archive of the good prior edition\n", encoding="utf-8")
        # a second pass over the same output root, with nothing verifiable
        out2, work2, pack_root2 = paths("keep-prior-2")
        original = ar.LiveWriter

        class MutantWriter(object):
            def __init__(self, config):
                self.config = config
                self.label = "stub/mutant"

            def write(self, prompt, system, section=None):
                return "Only 199 seats are in play."

        ar.LiveWriter = MutantWriter
        try:
            again = ar.run_authoring(lang="en", pack=KEDAH, out_root=out,
                                     work_root=work2, pack_root=pack_root2)
        finally:
            ar.LiveWriter = original
        self.assertEqual(result["published"], False)
        self.assertEqual(again["published"], False)
        self.assertIn("GOOD PRIOR", Path(edition).read_text(encoding="utf-8"))
        self.assertIn("archive of the good prior edition",
                      Path(archive).read_text(encoding="utf-8"))

    def test_a_partly_authored_edition_says_so_on_the_page(self):
        records = [{"key": "a", "number": 1, "title": "One", "body": "x",
                    "render": "authored"},
                   {"key": "b", "number": 2, "title": "Two", "body": "y",
                    "render": "chunked-partial"},
                   {"key": "c", "number": 3, "title": "Three", "body": "z",
                    "render": "degraded"}]
        for lang, expected_title, expected in (
                ("en", "AI-Authored", "2 of 3 sections AI-authored"),
                ("ms", "Ditulis AI", "2 daripada 3 bahagian ditulis AI")):
            markdown = ar.assemble_document(FEDERAL, records, lang, "stub/one",
                                            "pack.json", "hash")
            lines = [line for line in markdown.splitlines() if line.strip()]
            self.assertIn(expected_title, lines[0])
            self.assertIn(expected, lines[-1])
            self.assertTrue(ar.is_edition_footer(lines[-1]))
            self.assertNotIn("degraded", markdown)

    def test_an_edition_with_no_authored_section_is_not_branded_as_an_ai_edition(self):
        records = [{"key": "a", "number": 1, "title": "One", "body": "x",
                    "render": "degraded"},
                   {"key": "b", "number": 2, "title": "Two", "body": "y",
                    "render": "offline-fallback"}]
        for lang, expected in (("en", "GE16 — Model Report (deterministic render)"),
                               ("ms", "GE16 — Laporan Model (paparan deterministik)")):
            markdown = ar.assemble_document(FEDERAL, records, lang, "stub/one",
                                            "pack.json", "hash")
            lines = [line for line in markdown.splitlines() if line.strip()]
            self.assertEqual(lines[0], "# {0}".format(expected))
            self.assertNotIn("AI-Authored", markdown)
            self.assertNotIn("Ditulis AI", markdown)
            self.assertIn(lines[-1], ar.OFFLINE_FOOTERS)


class LabelMappingTests(unittest.TestCase):
    """F3: a registry token or a field name never reaches the reader."""

    CASES = (
        ("pn_core: Malay share floor", "PN core seat: Malay share floor"),
        ("mixed_malay: Malay share ceiling", "mixed-Malay seat: Malay share ceiling"),
        ("true_mixed: Malay share floor", "true mixed seat: Malay share floor"),
        ("non_malay: Malay share floor", "non-Malay seat: Malay share floor"),
        ("Seats typed pn_core", "PN core seat count"),
        ("Seats typed east-malaysia", "East Malaysia seat count"),
        ("Seats typed non_malay", "non-Malay seat count"),
        ("MACRO reading: gdp_yoy", "economic backdrop: GDP growth"),
        ("MACRO reading: cpi_yoy", "economic backdrop: inflation"),
        ("MACRO reading: ringgit", "economic backdrop: ringgit exchange rate"),
        ("Electorate share/demographic: wt_malay",
         "Electorate share/demographic: Malay share of the electorate"),
        ("GE15 seats inside the margin_under_5 margin band",
         "GE15 seats inside the under-5% margin band"),
        ("EVENT_SHOCKS is empty: no seat-level shocks are carried",
         "No seat-level electoral shocks are carried in this edition"),
        ("Model central estimate: government-aligned seats",
         "Model central estimate: government-aligned seats"),
        ("macrophage count in the sample", "macrophage count in the sample"),
        ("Monte Carlo 10th percentile: government-aligned seats",
         "Monte Carlo 10th percentile: government-aligned seats"),
    )

    def test_a_registry_token_reads_as_its_phrase(self):
        for raw, expected in self.CASES:
            self.assertEqual(ar.human_label(raw), expected, raw)

    def test_a_claim_with_no_label_falls_back_to_the_key_as_words(self):
        self.assertEqual(ar._human_label({"key": "macro:gdp_yoy", "label": ""}),
                         "economic backdrop")
        self.assertEqual(ar._human_label({"key": "seat_type_count:pn_core",
                                          "label": None}), "seat profile")
        self.assertEqual(ar._human_label({"key": "projection", "label": ""}),
                         "model central estimate")
        self.assertEqual(ar._human_label({"key": "threshold:x", "label": ""}),
                         "majority threshold")
        self.assertEqual(ar._human_label({"key": "threshold:simple_majority",
                                          "label": "Seats needed for a simple majority"}),
                         "Seats needed for a simple majority")

    def test_the_notes_line_for_a_seat_token_reads_as_words(self):
        section = next(s for s in FEDERAL["sections"] if s["key"] == "electorate")
        index = rp.claim_index(FEDERAL)
        body = ar.render_fallback(FEDERAL, section, index, "en")
        self.assertEqual(ar.verify_text(body, rp.allowed_tokens(FEDERAL), index), [])
        reader_text = spoken(body)
        for token in FORBIDDEN_PAGE_TOKENS:
            self.assertNotIn(token, reader_text, token)
        self.assertIn("PN core seat count: 51 seats.", reader_text)
        self.assertIn("economic backdrop", spoken(
            ar.render_fallback(FEDERAL, next(s for s in FEDERAL["sections"]
                                             if s["key"] == "intro_context"),
                               index, "en")))

    def test_the_fact_sheet_hands_the_writer_words_not_tokens(self):
        """The writer still gets the claim ids (it has to cite them); the label
        column — the only part it may echo into prose — carries no token."""
        index = rp.claim_index(FEDERAL)
        section = next(s for s in FEDERAL["sections"] if s["key"] == "electorate")
        sheet = ar.build_fact_sheet(FEDERAL, section, index, "en")
        labels = 0
        for line in sheet.splitlines():
            if not line.startswith("  - "):
                continue
            parts = line.split(" | ")
            self.assertEqual(len(parts), 4, line)
            labels += 1
            for token in FORBIDDEN_PAGE_TOKENS:
                self.assertNotIn(token, parts[1], line)
            self.assertNotIn("_", parts[1], line)
        self.assertGreater(labels, 0)
        self.assertIn("PN core seat count", sheet)

    def test_a_machine_unit_and_an_enum_value_read_as_words(self):
        """F3: ``text``/``date``/``seed`` are the pack's value KIND, not a unit of
        measure, and a tier is a phrase — neither reaches the page."""
        for machine in ("text", "date", "seed"):
            self.assertEqual(ar.reader_unit(machine, "en"), "", machine)
            self.assertEqual(ar.reader_unit(machine, "ms"), "", machine)
        self.assertEqual(ar.reader_unit("seats", "ms"), "kerusi")
        self.assertEqual(ar.reader_unit("seats", "en"), "seats")
        self.assertEqual(ar.reader_value("high_risk", "en"), "high risk")
        self.assertEqual(ar.reader_value("super_marginal", "en"), "highly marginal")
        self.assertEqual(ar.reader_value("high_risk", "ms"), "berisiko tinggi")
        self.assertEqual(ar.reader_value("2.48", "en"), "2.48",
                         "a figure is printed byte for byte")
        self.assertEqual(
            ar.cite_line({"value_str": "high_risk", "unit": "text",
                          "label": "Battleground tier"}, "en"),
            "high risk — Battleground tier")
        self.assertEqual(
            ar.cite_line({"value_str": "2026-09-24", "unit": "date",
                          "label": "Data as of"}, "en"),
            "2026-09-24 — Data as of")
        self.assertEqual(
            ar.cite_line({"value_str": "42", "unit": "seed",
                          "label": "Monte Carlo random seed"}, "ms"),
            "42 — Monte Carlo random seed")

    def test_the_fact_sheet_hands_the_writer_words_not_a_machine_value(self):
        """The writer echoes what the sheet shows, so the sheet shows the page."""
        index = rp.claim_index(FEDERAL)
        section = next(s for s in FEDERAL["sections"] if s["key"] == "watch_list")
        sheet = ar.build_fact_sheet(FEDERAL, section, index, "en")
        self.assertIn("value: high risk | unit: -", sheet)
        self.assertIn("value: highly marginal | unit: -", sheet)
        self.assertNotIn("value: high_risk", sheet)
        self.assertNotIn("value: super_marginal", sheet)
        self.assertNotIn("unit: text", sheet)
        self.assertNotIn("unit: date", sheet)
        self.assertNotIn("unit: seed", sheet)

    def test_a_raw_enum_in_the_prose_is_a_violation(self):
        """The belt to the fact sheet's brace: echoing the token does not verify."""
        self.assertEqual(
            [item["token"] for item in ar.reader_violations("The tier is high_risk.", "en")],
            ["high_risk"])
        self.assertEqual(
            [item["token"] for item in
             ar.reader_violations("The tier is super_marginal.", "en")],
            ["super_marginal"])
        self.assertEqual(ar.reader_violations("The tier is high risk.", "en"), [])
        self.assertEqual(ar.reader_violations("The tier is highly marginal.", "en"), [])
        self.assertEqual(
            ar.reader_violations("See [fed:watch_tier:P053:high_risk].", "en"), [],
            "an enum inside a citation id is the machinery's name for a claim")

    def test_the_watch_list_fallback_names_the_tier_in_words(self):
        index = rp.claim_index(FEDERAL)
        section = next(s for s in FEDERAL["sections"] if s["key"] == "watch_list")
        body = spoken(ar.render_fallback(FEDERAL, section, index, "en"))
        self.assertNotRegex(body, r"\b(high_risk|super_marginal)\b")
        self.assertIn("tier high risk", body)
        kedah = spoken(ar.render_fallback(KEDAH,
                                          next(s for s in KEDAH["sections"]
                                               if s["key"] == "watch_list"),
                                          rp.claim_index(KEDAH), "en"))
        self.assertNotRegex(kedah, r"\b(high_risk|super_marginal)\b")
        self.assertIn("tier highly marginal", kedah)

    def test_the_notes_guard_would_catch_a_raw_key(self):
        """The notes guards above are live: a new registry key would be caught."""
        self.assertIsNotNone(NOTES_KEY_PREFIX_RE.match("^40 KPI_registry: 51 seats"))
        self.assertIsNotNone(LABEL_KEY_PREFIX_RE.match("KPI_registry: 51 seats"))
        self.assertIsNotNone(
            re.search(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b", "^8 EVENT_SHOCKS: empty"))
        self.assertIsNotNone(
            re.search(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", "new_field: 3 seats"))
        entry = "^40 KPI_registry: 51 seats — new_field"
        self.assertIsNotNone(NOTES_KEY_PREFIX_RE.match(entry))
        self.assertIn("_", entry.split(" — ", 1)[1])


if __name__ == "__main__":
    unittest.main()


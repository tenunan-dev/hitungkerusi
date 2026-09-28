"""P2.2 identity tests: normalize_link.v1, evidence_id, judgment_id.

Run from the repository root:
    python3 -m pytest data/tests/test_p2_2_identity.py -q
"""
import importlib.util
import pathlib
import unittest

IMPORT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "import"


def load(name):
    spec = importlib.util.spec_from_file_location(name, IMPORT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


normalize_link = load("normalize_link")
identity = load("identity")


class NormalizeLinkTests(unittest.TestCase):
    def test_strips_tracking_parameters(self):
        self.assertEqual(
            normalize_link.normalize_link("https://Example.com/a/?utm_source=rss&utm_medium=rss&fbclid=x93"),
            "https://example.com/a/")

    def test_keeps_non_tracking_query_parameters(self):
        self.assertEqual(
            normalize_link.normalize_link("https://example.com/news?id=123&sort=asc&utm_campaign=z"),
            "https://example.com/news?id=123&sort=asc")

    def test_decodes_html_entities_before_splitting_query(self):
        # Real corpus shape: feed &#038;-encoded separators (V2 Utusan links).
        raw = ("https://www.utusan.com.my/nasional/2026/09/story/"
               "?utm_source=rss&#038;utm_medium=rss&#038;utm_campaign=story")
        self.assertEqual(
            normalize_link.normalize_link(raw),
            "https://www.utusan.com.my/nasional/2026/09/story/")

    def test_entity_decode_exposes_non_tracking_params(self):
        self.assertEqual(
            normalize_link.normalize_link("https://example.com/p?u=1&#038;utm_x=2"),
            "https://example.com/p?u=1")

    def test_lowercases_host_and_scheme_keeps_path_verbatim(self):
        self.assertEqual(
            normalize_link.normalize_link("HTTPS://News.Example.COM/Deep/Path/"),
            "https://news.example.com/Deep/Path/")

    def test_drops_fragment(self):
        self.assertEqual(
            normalize_link.normalize_link("https://example.com/a#section"),
            "https://example.com/a")

    def test_non_url_strings_pass_through(self):
        seen_key = "Ilham Centre|7 marginal seats - Free Malaysia Today"
        self.assertEqual(normalize_link.normalize_link(seen_key), seen_key)
        self.assertEqual(normalize_link.normalize_link("plain &#038; text"),
                         "plain & text")

    def test_pure_and_deterministic(self):
        sample = "https://example.com/a/?utm_source=rss&id=7"
        first = normalize_link.normalize_link(sample)
        for _ in range(5):
            self.assertEqual(normalize_link.normalize_link(sample), first)

    def test_version_is_recorded(self):
        self.assertEqual(normalize_link.NORMALIZER_VERSION, "normalize_link.v1")


class IdentityTests(unittest.TestCase):
    def test_evidence_id_format(self):
        value = identity.evidence_id("https://example.com/a")
        self.assertRegex(value, r"^ev[0-9a-f]{16}$")

    def test_evidence_id_deterministic_and_variant_stable(self):
        base = identity.evidence_id_for_link("https://example.com/a")
        self.assertEqual(base, identity.evidence_id_for_link("https://example.com/a"))
        # tracking-param variants collapse to the same evidence row
        self.assertEqual(base, identity.evidence_id_for_link("https://EXAMPLE.com/a?utm_source=x"))
        self.assertNotEqual(base, identity.evidence_id_for_link("https://example.com/b"))

    def test_judgment_id_format_and_components(self):
        base = identity.judgment_id("ev0123456789abcdef", "2026-09-26T06:08:12+00:00", "accept")
        self.assertRegex(base, r"^jg[0-9a-f]{12}$")
        self.assertEqual(base, identity.judgment_id("ev0123456789abcdef", "2026-09-26T06:08:12+00:00", "accept"))
        self.assertNotEqual(base, identity.judgment_id("ev0123456789abcdee", "2026-09-26T06:08:12+00:00", "accept"))
        self.assertNotEqual(base, identity.judgment_id("ev0123456789abcdef", "2026-09-26T06:08:13+00:00", "accept"))
        self.assertNotEqual(base, identity.judgment_id("ev0123456789abcdef", "2026-09-26T06:08:12+00:00", "reject"))

    def test_candidate_id_format(self):
        value = identity.candidate_id("PEJUANG", "party")
        self.assertRegex(value, r"^cand-[0-9a-f]{12}$")
        self.assertNotEqual(value, identity.candidate_id("PEJUANG", "bloc"))


if __name__ == "__main__":
    unittest.main()

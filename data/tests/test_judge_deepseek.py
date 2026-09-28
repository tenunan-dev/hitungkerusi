"""Contract tests for the deepseek news judge backend.

Hermetic: every HTTP call is stubbed (no network, no API key in the environment),
every artifact lives in a temp directory. The tests pin the four things the
baseline rebuild depends on:

  * the request shape is OpenAI-compatible chat.completions against DeepSeek
    (model ``deepseek-chat`` by default, ``DEEPSEEK_MODEL``/``DEEPSEEK_BASE_URL``
    overrides, key only in the Authorization header, never logged);
  * 429/5xx replies are retried with backoff and a batch that stays unjudgeable
    FAILS LOUD instead of writing a partial judgment;
  * the judge batch contract stays at <= 50 items, and the judged files it writes
    are consumed unchanged by the real collectors' ``--commit`` path
    (track_ge16_news.gather_judged / ge16_news_backfill.gather_judged);
  * judged evidence is bound to the batch it judged (source sha), so a stale
    judged file is re-judged rather than silently merged onto new candidates.

Run from the repository root:
    python3 -m pytest 1_DATA/tests/test_judge_deepseek.py -q
"""

import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
import urllib.error

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
COLLECTOR_ROOT = REPOSITORY_ROOT / "scripts" / "collect"
if str(COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_ROOT))

import ge16_judge_deepseek as JUDGE  # noqa: E402


def load_module(filename):
    path = COLLECTOR_ROOT / filename
    spec = importlib.util.spec_from_file_location("ge16_fixture_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch(obj, **attrs):
    for key, value in attrs.items():
        setattr(obj, key, value)


TRACKER = load_module("track_ge16_news.py")
BACKFILL = load_module("ge16_news_backfill.py")


class FakeResponse:
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def reply(verdicts):
    """A DeepSeek-shaped chat.completions response carrying a JSON verdict list."""
    content = json.dumps({"verdicts": verdicts})
    return FakeResponse(json.dumps({"choices": [{"message": {"content": content}}]}))


def all_accept(count=3, **extra):
    return [{"i": index + 1, "accept": True, "category": "election", **extra}
            for index in range(count)]


def items(count=3):
    return [{"title": f"Headline {index}", "desc": f"Body {index}",
             "date": "2026-09-25T08:00:00", "source": "The Star",
             "link": f"https://example.test/{index}"} for index in range(count)]


class JudgeBackendTestCase(unittest.TestCase):
    def setUp(self):
        self.workspace = pathlib.Path(tempfile.mkdtemp(prefix="ge16-judge-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.requests = []
        self.saved_env = {key: os.environ.get(key) for key in
                          (JUDGE.API_KEY_ENV, JUDGE.MODEL_ENV, JUDGE.BASE_URL_ENV)}
        for key in self.saved_env:
            os.environ.pop(key, None)
        os.environ[JUDGE.API_KEY_ENV] = "sk-fixture-key-value"

        def restore():
            for key, value in self.saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)

    def client(self, responses, sleep=None):
        """A ChatClient whose transport replays `responses` (exceptions allowed)."""
        queue = list(responses)

        def opener(request, timeout):
            self.requests.append({"url": request.full_url,
                                  "headers": dict(request.headers),
                                  "payload": json.loads(request.data.decode("utf-8"))})
            outcome = queue.pop(0) if queue else reply(all_accept())
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return JUDGE.ChatClient(opener=opener, sleep=sleep or (lambda seconds: None))

    @staticmethod
    def http_error(status, url="https://api.deepseek.com/chat/completions"):
        return urllib.error.HTTPError(url, status, "stub", {}, io.BytesIO(b'{"error":"stub"}'))


class RequestShapeTests(JudgeBackendTestCase):
    def test_default_model_and_openai_compatible_endpoint(self):
        client = self.client([reply(all_accept(2))])
        judge = JUDGE.DeepSeekJudge(client=client, vocabulary=("election", "analysis"))
        outcome = judge.judge_items(items(2))
        self.assertEqual(len(outcome["accepted"]), 2)
        request = self.requests[-1]
        self.assertEqual(request["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(request["payload"]["model"], "deepseek-chat")
        self.assertFalse(request["payload"]["stream"])
        self.assertEqual(request["payload"]["temperature"], 0.0)
        self.assertEqual(request["payload"]["response_format"], {"type": "json_object"})
        roles = [message["role"] for message in request["payload"]["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertIn("json", request["payload"]["messages"][1]["content"].lower())
        self.assertEqual(request["headers"].get("Authorization"), "Bearer sk-fixture-key-value")

    def test_model_and_base_url_environment_overrides(self):
        os.environ[JUDGE.MODEL_ENV] = "deepseek-reasoner"
        os.environ[JUDGE.BASE_URL_ENV] = "https://judge.internal/v1/"
        client = self.client([reply(all_accept(1))])
        self.assertEqual(client.model, "deepseek-reasoner")
        self.assertEqual(client.base, "https://judge.internal/v1")
        JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)).judge_items(items(1))
        self.assertEqual(self.requests[-1]["url"], "https://judge.internal/v1/chat/completions")
        self.assertEqual(self.requests[-1]["payload"]["model"], "deepseek-reasoner")

    def test_missing_credential_fails_loudly_without_leaking(self):
        os.environ.pop(JUDGE.API_KEY_ENV, None)
        with self.assertRaises(JUDGE.MissingCredentialError):
            JUDGE.api_key()
        client = self.client([reply(all_accept(1))])
        with self.assertRaises(JUDGE.MissingCredentialError):
            JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)).judge_items(items(1))
        self.assertEqual(self.requests, [], "no request may be attempted without a key")

    def test_key_never_appears_in_errors_or_reports(self):
        secret = os.environ[JUDGE.API_KEY_ENV]
        self.assertNotIn(secret, JUDGE.redact(f"boom {secret} boom"))
        client = self.client([self.http_error(401)] * 3)
        client.max_retries = 0
        with self.assertRaises(JUDGE.JudgeError) as caught:
            JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)).judge_items(items(1))
        self.assertNotIn(secret, str(caught.exception))


class RetryTests(JudgeBackendTestCase):
    def test_retries_on_rate_limit_and_server_errors(self):
        slept = []
        client = self.client([self.http_error(429), self.http_error(503),
                              self.http_error(500), reply(all_accept(2))],
                             sleep=slept.append)
        outcome = JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)).judge_items(items(2))
        self.assertEqual(len(outcome["accepted"]), 2)
        self.assertEqual(client.stats["retries"], 3)
        self.assertEqual(len(slept), 3)
        self.assertTrue(all(later > earlier for earlier, later in zip(slept, slept[1:])),
                        "backoff must grow")

    def test_retries_on_timeout_then_gives_up_loudly(self):
        client = self.client([urllib.error.URLError("timed out")] * 2 + [reply(all_accept(1))])
        client.max_retries = 2
        outcome = JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)).judge_items(items(1))
        self.assertEqual(outcome["accepted_count"], 1)

    def test_non_retryable_status_fails_immediately(self):
        client = self.client([self.http_error(400)])
        with self.assertRaises(JUDGE.JudgeError):
            JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)).judge_items(items(1))
        self.assertEqual(client.stats["calls"], 1)

    def test_exhausted_retries_raise_and_write_nothing(self):
        batch_path = self.workspace / "ge16-news-judge-batch-1.json"
        out_path = self.workspace / "ge16-news-judged-batch-1.json"
        JUDGE.atomic_write_json(batch_path, {"batch": 1, "items": items(2)})
        client = self.client([self.http_error(503)] * 4)
        client.max_retries = 2
        with self.assertRaises(JUDGE.JudgeError):
            JUDGE.judge_batch_file(batch_path, out_path,
                                   JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)))
        self.assertFalse(out_path.exists(), "a failed batch must not be written")


class JsonGuardTests(JudgeBackendTestCase):
    def test_parse_from_fences_prose_and_bare_object(self):
        self.assertEqual(JUDGE.extract_json('```json\n{"verdicts": []}\n```'), {"verdicts": []})
        self.assertEqual(JUDGE.extract_json('Here: [{"i": 1}] done')[-1]["i"], 1)
        self.assertEqual(JUDGE.extract_json('{"verdicts": [{"i": 1, "accept": true}]}')["verdicts"][0]["i"], 1)

    def test_unparsable_reply_is_repaired_then_refused(self):
        client = self.client([FakeResponse(json.dumps({"choices": [{"message": {"content": "not json"}}]})),
                              FakeResponse(json.dumps({"choices": [{"message": {"content": "still not json"}}]}))])
        judge = JUDGE.DeepSeekJudge(client=client, vocabulary=("election",), repair_attempts=1)
        with self.assertRaises(JUDGE.JudgeError):
            judge.judge_items(items(1))

    def test_empty_choices_are_refused(self):
        client = self.client([FakeResponse(json.dumps({"choices": []}))])
        with self.assertRaises(JUDGE.JudgeError):
            JUDGE.DeepSeekJudge(client=client, vocabulary=("election",)).judge_items(items(1))

    def test_category_and_language_are_coerced_into_the_vocabulary(self):
        verdicts = [{"i": 1, "accept": True, "category": "not-a-category", "lang": "fr"},
                    {"i": 2, "accept": False, "reason": "sport"}]
        client = self.client([reply(verdicts)])
        outcome = JUDGE.DeepSeekJudge(client=client, vocabulary=("election", "analysis")).judge_items(items(2))
        self.assertEqual([entry["title"] for entry in outcome["accepted"]], ["Headline 0"])
        self.assertEqual(outcome["accepted"][0]["category"], "analysis")
        self.assertEqual(outcome["accepted"][0]["lang"], "en")
        self.assertEqual(outcome["rejected_indexes"], [2])
        self.assertEqual(outcome["notes"]["category_coerced"], ["not-a-category"])

    def test_omitted_indexes_are_repaired_and_never_silently_dropped(self):
        first = reply([{"i": 1, "accept": True, "category": "election"}])
        second = reply([{"i": 2, "accept": False}, {"i": 3, "accept": True, "category": "election"}])
        client = self.client([first, second])
        judge = JUDGE.DeepSeekJudge(client=client, vocabulary=("election",), repair_attempts=1)
        outcome = judge.judge_items(items(3))
        self.assertEqual(outcome["completions"], 2)
        self.assertEqual(len(outcome["accepted"]), 2)
        self.assertEqual(outcome["rejected_indexes"], [2])
        self.assertIn("Rule on EVERY index", self.requests[-1]["payload"]["messages"][1]["content"])

    def test_omitted_indexes_after_the_repair_budget_fail_closed(self):
        client = self.client([reply([{"i": 1, "accept": True}])] * 3)
        judge = JUDGE.DeepSeekJudge(client=client, vocabulary=("election",), repair_attempts=1)
        with self.assertRaises(JUDGE.IncompleteVerdictsError):
            judge.judge_items(items(3))


class BatchContractTests(JudgeBackendTestCase):
    def test_oversized_item_list_is_refused(self):
        judge = JUDGE.DeepSeekJudge(client=self.client([]), vocabulary=("election",))
        with self.assertRaises(JUDGE.OversizedBatchError):
            judge.judge_items(items(JUDGE.MAX_BATCH_ITEMS + 1))
        self.assertEqual(self.requests, [])

    def test_oversized_batch_file_is_refused_before_any_call(self):
        batch_path = self.workspace / "big.json"
        JUDGE.atomic_write_json(batch_path, {"batch": 1, "items": items(51)})
        with self.assertRaises(JUDGE.OversizedBatchError):
            JUDGE.judge_batch_file(batch_path, self.workspace / "out.json",
                                   JUDGE.DeepSeekJudge(client=self.client([]),
                                                       vocabulary=("election",)))
        self.assertEqual(self.requests, [])

    def test_judged_file_carries_the_batch_sha_and_is_reused(self):
        batch_path = self.workspace / "ge16-news-judge-batch-1.json"
        out_path = self.workspace / "ge16-news-judged-batch-1.json"
        JUDGE.atomic_write_json(batch_path, {"schema": "ge16.news-backfill-judge-batch.v1",
                                             "batch": 1, "collection_id": "cycle-1",
                                             "count": 2, "items": items(2)})
        judge = JUDGE.DeepSeekJudge(client=self.client([reply(all_accept(2))]),
                                    vocabulary=("election",))
        first = JUDGE.judge_batch_file(batch_path, out_path, judge)
        self.assertEqual(first["status"], "judged")
        payload = json.loads(out_path.read_text())
        self.assertEqual(payload["source_sha256"], JUDGE.sha256_file(batch_path))
        self.assertEqual(payload["accepted_count"], 2)
        self.assertEqual(len(payload["accepted"]), 2)
        self.assertEqual(payload["judged_by"], "deepseek")
        self.assertEqual(payload["judge_model"], "deepseek-chat")
        # the same batch is reused without another call
        second = JUDGE.judge_batch_file(batch_path, out_path, judge)
        self.assertEqual(second["status"], "already-judged")
        self.assertEqual(len(self.requests), 1)
        # a changed batch is stale evidence: it must be re-judged
        payload_items = items(2)
        JUDGE.atomic_write_json(batch_path, {"batch": 1, "items": payload_items + items(1)})
        third = JUDGE.judge_batch_file(batch_path, out_path, judge)
        self.assertEqual(third["status"], "judged")
        self.assertEqual(len(self.requests), 2)

    def test_pipeline_batches_respect_the_fifty_item_ceiling(self):
        tracker = self.workspace / "research" / "trackers"
        tracker.mkdir(parents=True)
        for number, chunk in ((1, items(50)), (2, items(7))):
            JUDGE.atomic_write_json(tracker / f"ge16-news-backfill-judge-batch-{number}.json",
                                    {"schema": "ge16.news-backfill-judge-batch.v1",
                                     "batch": number, "collection_id": "cycle-1",
                                     "count": len(chunk), "items": chunk})
        JUDGE.atomic_write_json(tracker / "ge16-news-backfill-judge-manifest.json", {
            "schema": "ge16.news-backfill-manifest.v1", "collection_id": "cycle-1",
            "total": 57, "batch_size": 50, "batch_count": 2,
            "batches": [{"batch": 1, "file": "ge16-news-backfill-judge-batch-1.json", "count": 50},
                        {"batch": 2, "file": "ge16-news-backfill-judge-batch-2.json", "count": 7}]})
        judge = JUDGE.DeepSeekJudge(
            client=self.client([reply(all_accept(50)), reply(all_accept(7))]),
            vocabulary=("election",))
        summary = JUDGE.judge_pipeline("backfill", str(tracker), judge, log=lambda *a: None)
        self.assertEqual(summary["batches"], 2)
        self.assertEqual(summary["judged"], 2)
        for request in self.requests:
            self.assertLessEqual(len(request["payload"]["messages"][1]["content"].split('"i":')),
                                 51)


class CollectorContractTests(JudgeBackendTestCase):
    """The judged files this backend writes must feed the real --commit path."""

    def setUp(self):
        super().setUp()
        self.tracker_dir = self.workspace / "research" / "trackers"
        self.tracker_dir.mkdir(parents=True)
        patch(TRACKER,
              DIR=str(self.tracker_dir),
              CANDIDATES=str(self.tracker_dir / "ge16-news-candidates.json"),
              JUDGED=str(self.tracker_dir / "ge16-news-judged.json"),
              JUDGE_MANIFEST=str(self.tracker_dir / "ge16-news-judge-manifest.json"),
              JUDGED_BATCH_FMT=str(self.tracker_dir / "ge16-news-judged-batch-%d.json"),
              JUDGED_BATCH_GLOB=str(self.tracker_dir / "ge16-news-judged-batch-*.json"))
        patch(BACKFILL,
              DIR=str(self.tracker_dir),
              CANDIDATES=str(self.tracker_dir / "ge16-news-backfill-candidates.json"),
              JUDGE_MANIFEST=str(self.tracker_dir / "ge16-news-backfill-judge-manifest.json"),
              JUDGED_BATCH_FMT=str(self.tracker_dir / "ge16-news-backfill-judged-batch-%d.json"))

    def _judge_tracker_batches(self, count=2):
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-candidates.json",
                                {"generated_at": "cycle-1", "count": count, "items": items(count)})
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-judge-batch-1.json",
                                {"schema": "ge16.news-judge-batch.v1", "batch": 1,
                                 "collection_id": "cycle-1", "count": count, "items": items(count)})
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-judge-manifest.json",
                                {"collection_id": "cycle-1", "batch_size": 50, "total": count,
                                 "batch_count": 1,
                                 "batches": [{"batch": 1, "file": "ge16-news-judge-batch-1.json",
                                              "count": count,
                                              "judged_file": "ge16-news-judged-batch-1.json"}]})
        judge = JUDGE.DeepSeekJudge(client=self.client([reply(all_accept(count))]),
                                    vocabulary=("election",))
        return JUDGE.judge_pipeline("tracker", str(self.tracker_dir), judge,
                                    consolidate_path=str(self.tracker_dir / "ge16-news-judged.json"),
                                    log=lambda *a: None)

    def test_tracker_gather_judged_reads_the_judged_batches(self):
        self._judge_tracker_batches()
        accepted, source, _manifest = TRACKER.gather_judged()
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0]["title"], "Headline 0")
        self.assertTrue(all("category" in entry for entry in accepted))

    def test_consolidated_payload_is_read_first_and_is_legal_when_empty(self):
        self._judge_tracker_batches()
        accepted, source, _manifest = TRACKER.gather_judged()
        self.assertEqual(source, "ge16-news-judged.json")
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-judged.json",
                                {"generated_at": "cycle-1", "count": 0, "accepted": []})
        accepted, _source, _manifest = TRACKER.gather_judged()
        self.assertEqual(accepted, [], "an explicit zero-acceptance cycle stays legal")

    def test_partial_judgment_still_refuses_to_commit(self):
        self._judge_tracker_batches()
        (self.tracker_dir / "ge16-news-judged.json").unlink()
        (self.tracker_dir / "ge16-news-judged-batch-1.json").unlink()
        with self.assertRaises(TRACKER.PartialJudgmentError):
            TRACKER.gather_judged()

    def test_backfill_gather_judged_reads_the_judged_batches(self):
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-backfill-judge-batch-1.json",
                                {"schema": "ge16.news-backfill-judge-batch.v1", "batch": 1,
                                 "collection_id": "cycle-1", "count": 2, "items": items(2)})
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-backfill-judge-manifest.json",
                                {"collection_id": "cycle-1", "batch_size": 50, "total": 2,
                                 "batch_count": 1,
                                 "batches": [{"batch": 1,
                                              "file": "ge16-news-backfill-judge-batch-1.json",
                                              "count": 2}]})
        judge = JUDGE.DeepSeekJudge(client=self.client([reply(all_accept(2))]),
                                    vocabulary=("election",))
        JUDGE.judge_pipeline("backfill", str(self.tracker_dir), judge, log=lambda *a: None)
        judged, numbers = BACKFILL.gather_judged()
        self.assertEqual(numbers, [1])
        self.assertEqual(len(judged), 2)
        self.assertTrue(all("title" in entry for entry in judged))

    def test_backfill_refuses_a_partially_judged_sweep(self):
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-backfill-judge-manifest.json",
                                {"collection_id": "cycle-1", "batch_count": 2,
                                 "batches": [{"batch": 1, "count": 1}, {"batch": 2, "count": 1}]})
        JUDGE.atomic_write_json(self.tracker_dir / "ge16-news-backfill-judged-batch-1.json",
                                {"batch": 1, "accepted": []})
        judged, numbers = BACKFILL.gather_judged()
        self.assertIsNone(judged)
        self.assertEqual(numbers, [1, 2])


if __name__ == "__main__":
    unittest.main()

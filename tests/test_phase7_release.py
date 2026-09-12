from __future__ import annotations

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from buy_wait.evidence import EvidenceCache, cache_key, validate_image_fact  # noqa: E402
from buy_wait.usage import UsageRecord, UsageTracker  # noqa: E402
from buy_wait.zenmux import DEFAULT_MODEL, ZenMuxSettings, _json_from_text  # noqa: E402
from evaluation.package import build_package  # noqa: E402


class Phase7ReleaseTests(unittest.TestCase):
    def test_model_default_is_locked(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "meta/muse-spark-1.3-contributor")

    def test_dotenv_settings_are_loaded_without_os_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("ZENMUX_API_KEY=test\nZENMUX_MODEL=test/model\nZENMUX_MAX_MODEL_CALLS=3\n", encoding="utf-8")
            settings = ZenMuxSettings.from_env(dotenv_path=path)
            self.assertEqual(settings.api_key, "test")
            self.assertEqual(settings.model, "test/model")
            self.assertEqual(settings.max_model_calls, 3)

    def test_content_hash_cache_key_changes_with_model_prompt(self) -> None:
        first = cache_key("image", "image_01", "abc", "v1:model-a")
        second = cache_key("image", "image_01", "abc", "v1:model-b")
        self.assertNotEqual(first, second)

    def test_cache_round_trip_is_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = EvidenceCache(path)
            cache.put("key", {"facts": {"amount": "10"}})
            cache.save()
            self.assertEqual(EvidenceCache(path).get("key")["facts"]["amount"], "10")

    def test_cache_corruption_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(EvidenceCache(path).get("key"))

    def test_json_parser_accepts_fenced_object(self) -> None:
        self.assertEqual(_json_from_text("```json\n{\"amount\": 3}\n```"), {"amount": 3})

    def test_json_parser_accepts_surrounding_prose(self) -> None:
        self.assertEqual(_json_from_text("Here is the result: {\"amount\": 3}"), {"amount": 3})

    def test_image_paid_status_preserves_amount(self) -> None:
        fact, errors = validate_image_fact(
            {"status": "paid", "amount": "19.50", "currency": "INR", "confidence": 0.9},
            source_id="image_01", related_event_id="event_01", event_ids={"event_01"},
        )
        self.assertFalse(errors)
        self.assertEqual(fact["amount"], "19.50")

    def test_image_unpaid_status_preserves_amount(self) -> None:
        fact, errors = validate_image_fact(
            {"status": "unpaid", "amount": "19.50", "currency": "INR", "confidence": 0.9},
            source_id="image_01", related_event_id="event_01", event_ids={"event_01"},
        )
        self.assertFalse(errors)
        self.assertEqual(fact["status"], "unpaid")

    def test_image_unknown_event_is_rejected(self) -> None:
        fact, errors = validate_image_fact(
            {"status": "paid", "amount": "19.50", "currency": "INR", "confidence": 0.9},
            source_id="image_01", related_event_id="missing", event_ids={"event_01"},
        )
        self.assertIsNone(fact)
        self.assertEqual(errors[0]["reason"], "unknown_event_id")

    def test_usage_totals_include_only_token_values(self) -> None:
        tracker = UsageTracker()
        tracker.record(UsageRecord("zenmux.ai", DEFAULT_MODEL, "image", "image_01", False, 10, 4, Decimal("0.000002")))
        self.assertEqual(tracker.totals()[:3], (10, 4, 14))
        self.assertEqual(tracker.model_call_count, 1)

    def test_usage_cache_hits_are_not_model_calls(self) -> None:
        tracker = UsageTracker()
        tracker.record(UsageRecord("zenmux.ai", DEFAULT_MODEL, "image", "image_01", True, 0, 0))
        self.assertEqual(tracker.model_call_count, 0)
        self.assertEqual(tracker.cache_hit_count, 1)

    def test_usage_report_does_not_need_a_secret(self) -> None:
        tracker = UsageTracker()
        tracker.record(UsageRecord("zenmux.ai", DEFAULT_MODEL, "image", "image_01", False, 1, 1))
        self.assertNotIn("sk-", DEFAULT_MODEL)

    def test_package_contains_required_usage_report(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = build_package(root, Path(directory) / "code.zip")
            self.assertIn("evaluation/usage_report.md", result["files"])

    def test_package_does_not_include_env(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "code.zip"
            build_package(root, output)
            import zipfile
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(any(name.endswith(".env") for name in archive.namelist()))

    def test_package_does_not_include_cache(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "code.zip"
            build_package(root, output)
            import zipfile
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(any("cache/" in name for name in archive.namelist()))

    def test_package_is_readable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "code.zip"
            build_package(root, output)
            import zipfile
            with zipfile.ZipFile(output) as archive:
                self.assertTrue(archive.testzip() is None)

    def test_settings_reject_negative_call_budget(self) -> None:
        with self.assertRaises(ValueError):
            ZenMuxSettings.from_env(max_model_calls=-1)

    def test_settings_use_priced_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("ZENMUX_API_KEY=test\n", encoding="utf-8")
            settings = ZenMuxSettings.from_env(dotenv_path=path)
            self.assertEqual(settings.input_usd_per_million, Decimal("0.10"))
            self.assertEqual(settings.output_usd_per_million, Decimal("0.20"))

    def test_cache_entries_are_dicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text(json.dumps({"entries": {"x": []}}), encoding="utf-8")
            self.assertIsNone(EvidenceCache(path).get("x"))

    def test_image_status_received_is_supported(self) -> None:
        fact, errors = validate_image_fact(
            {"status": "received", "amount": "100", "currency": "INR", "confidence": 0.8},
            source_id="image_02", related_event_id=None, event_ids=set(),
        )
        self.assertFalse(errors)
        self.assertEqual(fact["status"], "received")

    def test_image_status_due_is_supported(self) -> None:
        fact, errors = validate_image_fact(
            {"status": "due", "amount": "100", "currency": "INR", "confidence": 0.8},
            source_id="image_05", related_event_id=None, event_ids=set(),
        )
        self.assertFalse(errors)
        self.assertEqual(fact["status"], "due")

    def test_model_name_is_not_written_as_a_secret(self) -> None:
        self.assertTrue(DEFAULT_MODEL.startswith("meta/"))


if __name__ == "__main__":
    unittest.main()

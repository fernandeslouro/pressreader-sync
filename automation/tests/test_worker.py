import importlib.util
import sys
import tempfile
import unittest
import zipfile
import json
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "pressreader_worker.py"
sys.path.insert(0, str(MODULE_PATH.parent))


class WorkerHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            spec = importlib.util.spec_from_file_location("pressreader_worker", MODULE_PATH)
            cls.worker = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = cls.worker
            assert spec.loader
            spec.loader.exec_module(cls.worker)
        except ModuleNotFoundError as err:
            if err.name == "playwright":
                raise unittest.SkipTest("playwright is not installed")
            raise

    def test_safe_component(self):
        self.assertEqual(self.worker.safe_component('  The / Daily: News  '), "The _ Daily_ News")
        self.assertEqual(self.worker.safe_component("..."), "Publication")

    def test_issue_date(self):
        self.assertEqual(self.worker.parse_issue_date("Issue Date 18 Jul 2026"), "2026-07-18")
        self.assertIsNone(self.worker.parse_issue_date("Issue date unavailable"))

    def test_retry_delays_continue_every_three_hours(self):
        self.assertEqual(
            [self.worker.retry_delay_seconds(value) for value in range(1, 7)],
            [600, 1800, 3600, 10800, 10800, 10800],
        )

    def test_failures_are_retried_and_reset_for_a_new_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            state = self.worker.StateStore(Path(temp))
            publication = self.worker.PublicationLink("Daily", "https://pressreader.com/daily")
            first = state.record_failure(publication, "temporary", "2026-07-28", now=1000)
            second = state.record_failure(publication, "temporary", "2026-07-28", now=2000)
            new_issue = state.record_failure(publication, "temporary", "2026-07-29", now=3000)

            self.assertEqual(first["next_retry_timestamp"], 1600)
            self.assertEqual(second["next_retry_timestamp"], 3800)
            self.assertEqual(new_issue["failures"], 1)
            self.assertEqual(new_issue["next_retry_timestamp"], 3600)
            self.assertTrue(state.retry_due(publication.url, now=3600))
            self.assertEqual(json.loads((Path(temp) / "retries.json").read_text())[
                self.worker.normalized_url(publication.url)
            ]["issue_date"], "2026-07-29")

            state.clear_failure(publication.url)
            self.assertFalse(state.retry_due(publication.url, now=9999))

    def test_export_devices_are_ordered_and_deduplicated(self):
        with mock.patch.dict(
            self.worker.os.environ,
            {"PRESSREADER_SYNC_EXPORT_DEVICES": "Nook, Kobo, nook, Sony"},
        ):
            self.assertEqual(self.worker.export_devices(), ["Nook", "Kobo", "Sony"])

    def test_legacy_preferred_device_keeps_fallbacks(self):
        with mock.patch.dict(
            self.worker.os.environ,
            {"PRESSREADER_SYNC_EXPORT_DEVICE": "Kobo"},
            clear=True,
        ):
            self.assertEqual(self.worker.export_devices(), ["Kobo", "Sony"])

    def test_epub_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            good = Path(temp) / "good.epub"
            bad = Path(temp) / "bad.epub"
            with zipfile.ZipFile(good, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip")
            bad.write_text("not an epub", encoding="utf-8")
            self.assertTrue(self.worker.is_epub(good))
            self.assertFalse(self.worker.is_epub(bad))

    def test_launch_context_uses_configured_proxy(self):
        playwright = mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(
                self.worker.os.environ,
                {"PRESSREADER_SYNC_PROXY": "http://10.203.0.2:8888"},
            ):
                self.worker.launch_context(playwright, Path(temp) / "profile", True)
        options = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(options["proxy"], {"server": "http://10.203.0.2:8888"})


if __name__ == "__main__":
    unittest.main()

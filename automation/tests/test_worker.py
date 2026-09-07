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

    def test_manual_run_requests_are_consumed(self):
        with tempfile.TemporaryDirectory() as temp:
            trigger = Path(temp) / "run-requested"
            self.assertFalse(self.worker.consume_run_request(trigger))
            trigger.touch()
            self.assertTrue(self.worker.consume_run_request(trigger))
            self.assertFalse(trigger.exists())
            self.assertFalse(self.worker.consume_run_request(trigger))

    def test_publication_queue_consumes_one_title_at_a_time(self):
        with tempfile.TemporaryDirectory() as temp:
            trigger = Path(temp) / "custom-trigger"
            queue = trigger.with_name("custom-trigger.publications")
            queue.mkdir()
            for name, title in [("a", "Daily"), ("b", "Weekly")]:
                (queue / (name + ".json")).write_text(json.dumps({"title": title}))
            self.assertEqual(self.worker.consume_publication_request(trigger), "Daily")
            self.assertEqual(self.worker.consume_publication_request(trigger), "Weekly")
            self.assertEqual(self.worker.consume_publication_request(trigger), "")
            self.assertEqual(list(queue.iterdir()), [])

    def test_targeted_cycle_uses_correct_proxy_and_bypasses_retry_filter(self):
        with mock.patch.dict(self.worker.os.environ, {
            "PRESSREADER_SYNC_SPECIAL_PROXY": "http://proxy:8888",
            "PRESSREADER_SYNC_SPECIAL_TITLE": "Special",
        }), mock.patch.object(self.worker, "run_once") as run:
            for title, proxy in [("Special", "http://proxy:8888"), ("Daily", "")]:
                run.reset_mock()
                self.worker.run_cycle(None, None, None, None, "catalog", only_title=title, retry_only=True)
                run.assert_called_once_with(
                    None, None, None, None, "catalog", 0,
                    proxy_server=proxy, only_title=title, retry_only=False,
                )

    def test_targeted_run_exports_only_matching_publication(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(self.worker, "sync_playwright"), \
                mock.patch.object(self.worker, "launch_context"), \
                mock.patch.object(self.worker, "PressReaderAutomation") as automation:
            instance = automation.return_value
            selected = self.worker.PublicationLink("Daily", "https://pressreader.com/daily")
            instance.discover_my_publications.return_value = [
                selected, self.worker.PublicationLink("Weekly", "https://pressreader.com/weekly")
            ]
            instance.export_latest.return_value = "exported"
            root = Path(temp)
            result = self.worker.run_once(root, root, root, root, "catalog", only_title="Daily")
            instance.export_latest.assert_called_once_with(selected)
            self.assertEqual(result.exported, 1)
            self.assertEqual(result.full_fetch_finished_at, "")
            instance.export_latest.reset_mock()
            result = self.worker.run_once(root, root, root, root, "catalog", only_title="Missing")
            instance.export_latest.assert_not_called()
            self.assertEqual(result.state, "error")

    def test_trigger_defaults_to_the_configured_state_directory(self):
        args = self.worker.parse_args(["run", "--state", "/tmp/custom-state"])
        trigger = args.trigger or args.state / "run-requested"
        self.assertEqual(trigger, Path("/tmp/custom-state/run-requested"))

    def test_retry_status_preserves_last_full_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            state = self.worker.StateStore(Path(temp))
            state.write_status(self.worker.RunStatus(
                state="ok", full_fetch_finished_at="2026-09-03T00:00:00+00:00"
            ))
            state.write_status(self.worker.RunStatus(
                state="ok", finished_at="2026-09-03T01:00:00+00:00"
            ))
            saved = json.loads((Path(temp) / "worker-status.json").read_text())
            self.assertEqual(
                saved["full_fetch_finished_at"], "2026-09-03T00:00:00+00:00"
            )

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

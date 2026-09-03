import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "pressreader_sync_bridge.py"
SPEC = importlib.util.spec_from_file_location("pressreader_sync_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        paper = root / "Daily Paper"
        paper.mkdir()
        (paper / "publication.json").write_text(
            json.dumps({"title": "The Daily Paper", "language": "en"}), encoding="utf-8"
        )
        (paper / "2026-07-18.pdf").write_bytes(b"older")
        (paper / "2026-07-19.epub").write_bytes(b"newest")
        (paper / "ignore.txt").write_text("no", encoding="utf-8")
        index = bridge.LibraryIndex(root, cache_seconds=60)
        index.rebuild()
        self.worker_status = root / "worker-status.json"
        self.worker_trigger = root / "run-requested"
        self.worker_status.write_text('{"state":"ok","exported":1}', encoding="utf-8")
        self.server = bridge.PressReaderSyncServer(
            ("127.0.0.1", 0), index, "secret", self.worker_status, self.worker_trigger
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, token="secret", method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req) as response:
            return response.status, response.headers, response.read()

    def test_catalog_latest_and_download(self):
        _, _, raw = self.request("/v1/publications")
        publications = json.loads(raw)["publications"]
        self.assertEqual(publications[0]["title"], "The Daily Paper")
        self.assertEqual(publications[0]["issue_count"], 2)
        publication_id = publications[0]["id"]

        _, _, raw = self.request(f"/v1/latest?publication={publication_id}")
        latest = json.loads(raw)["issue"]
        self.assertEqual(latest["date"], "2026-07-19")
        _, headers, body = self.request(latest["download_url"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, b"newest")

    def test_authentication_is_required(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("/v1/status", token=None)
        self.assertEqual(context.exception.code, 401)

    def test_status_includes_automation_health(self):
        _, _, raw = self.request("/v1/status")
        payload = json.loads(raw)
        self.assertEqual(payload["name"], "PressReader Sync Bridge")
        self.assertEqual(payload["automation"]["state"], "ok")
        self.assertEqual(payload["automation"]["exported"], 1)

    def test_status_ages_the_full_fetch_instead_of_a_retry(self):
        full_fetch = datetime.now(timezone.utc) - timedelta(seconds=90)
        retry = datetime.now(timezone.utc) - timedelta(seconds=10)
        self.worker_status.write_text(
            json.dumps({
                "state": "ok",
                "finished_at": retry.isoformat(),
                "full_fetch_finished_at": full_fetch.isoformat(),
            }),
            encoding="utf-8",
        )
        _, _, raw = self.request("/v1/status")
        automation = json.loads(raw)["automation"]
        age = automation["full_fetch_finished_seconds_ago"]
        self.assertNotIn("finished_seconds_ago", automation)
        self.assertGreaterEqual(age, 89)
        self.assertLessEqual(age, 92)

    def test_automation_run_request_creates_trigger(self):
        status, _, raw = self.request("/v1/automation/run", method="POST")
        payload = json.loads(raw)
        self.assertEqual(status, 202)
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["state"], "queued")
        self.assertTrue(self.worker_trigger.is_file())

    def test_automation_run_request_is_coalesced_while_running(self):
        self.worker_status.write_text('{"state":"running"}', encoding="utf-8")
        status, _, raw = self.request("/v1/automation/run", method="POST")
        payload = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertFalse(payload["accepted"])
        self.assertFalse(self.worker_trigger.exists())

    def test_unknown_ids_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("/v1/files/not-real")
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()

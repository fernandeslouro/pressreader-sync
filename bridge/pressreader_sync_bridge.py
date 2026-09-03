#!/usr/bin/env python3
"""Read-only local library bridge for the PressReader Sync KOReader plugin."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

SUPPORTED_FORMATS = {".pdf", ".epub", ".cbz", ".djvu"}
DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}(?:-\d{2})?)")


def opaque_id(kind: str, relative_path: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{relative_path}".encode("utf-8")).hexdigest()
    return digest[:24]


def date_from_name(name: str, mtime: float) -> str:
    match = DATE_RE.search(name)
    if match:
        return match.group("date")
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


@dataclass(frozen=True)
class Issue:
    id: str
    publication_id: str
    title: str
    date: str
    format: str
    size_bytes: int
    filename: str
    download_url: str
    path: Path

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("path")
        return data


@dataclass(frozen=True)
class Publication:
    id: str
    title: str
    language: str
    source_url: str
    issue_count: int
    latest_date: str


class LibraryIndex:
    def __init__(self, root: Path, cache_seconds: float = 5.0):
        self.root = root.resolve()
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._built_at = 0.0
        self._publications: list[Publication] = []
        self._issues_by_publication: dict[str, list[Issue]] = {}
        self._issues_by_id: dict[str, Issue] = {}

    def _read_metadata(self, folder: Path) -> dict[str, str]:
        metadata_file = folder / "publication.json"
        if not metadata_file.is_file():
            return {}
        try:
            raw = json.loads(metadata_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            return {key: str(raw.get(key, "")) for key in ("title", "language", "source_url")}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def rebuild(self) -> None:
        publications: list[Publication] = []
        issues_by_publication: dict[str, list[Issue]] = {}
        issues_by_id: dict[str, Issue] = {}

        if self.root.is_dir():
            folders = sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold())
        else:
            folders = []

        for folder in folders:
            relative_folder = folder.relative_to(self.root).as_posix()
            publication_id = opaque_id("publication", relative_folder)
            metadata = self._read_metadata(folder)
            issues: list[Issue] = []
            for path in folder.iterdir():
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_FORMATS:
                    continue
                resolved_path = path.resolve()
                try:
                    resolved_path.relative_to(self.root)
                except ValueError:
                    # Never index a symlink that escapes the configured library.
                    continue
                stat = path.stat()
                relative_file = path.relative_to(self.root).as_posix()
                issue_id = opaque_id("issue", relative_file)
                issue = Issue(
                    id=issue_id,
                    publication_id=publication_id,
                    title=path.stem,
                    date=date_from_name(path.stem, stat.st_mtime),
                    format=path.suffix.lower()[1:],
                    size_bytes=stat.st_size,
                    filename=path.name,
                    download_url=f"/v1/files/{quote(issue_id)}",
                    path=resolved_path,
                )
                issues.append(issue)
                issues_by_id[issue.id] = issue
            if not issues:
                continue
            issues.sort(key=lambda item: (item.date, item.filename.casefold()), reverse=True)
            publications.append(Publication(
                id=publication_id,
                title=metadata.get("title") or folder.name,
                language=metadata.get("language", ""),
                source_url=metadata.get("source_url", ""),
                issue_count=len(issues),
                latest_date=issues[0].date,
            ))
            issues_by_publication[publication_id] = issues

        publications.sort(key=lambda item: (item.latest_date, item.title.casefold()), reverse=True)
        self._publications = publications
        self._issues_by_publication = issues_by_publication
        self._issues_by_id = issues_by_id
        self._built_at = time.monotonic()

    def refresh(self) -> None:
        with self._lock:
            if time.monotonic() - self._built_at >= self.cache_seconds:
                self.rebuild()

    def publications(self) -> list[Publication]:
        self.refresh()
        return self._publications

    def issues(self, publication_id: str) -> list[Issue] | None:
        self.refresh()
        return self._issues_by_publication.get(publication_id)

    def issue(self, issue_id: str) -> Issue | None:
        self.refresh()
        return self._issues_by_id.get(issue_id)


class PressReaderSyncServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        index: LibraryIndex,
        token: str,
        worker_status: Path | None = None,
        worker_trigger: Path | None = None,
    ):
        super().__init__(address, PressReaderSyncHandler)
        self.index = index
        self.token = token
        self.worker_status = worker_status
        self.worker_trigger = worker_trigger


class PressReaderSyncHandler(BaseHTTPRequestHandler):
    server: PressReaderSyncServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def _authorised(self) -> bool:
        if not self.server.token:
            return True
        expected = f"Bearer {self.server.token}"
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, expected)

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _automation_status(self) -> dict[str, Any] | None:
        if not self.server.worker_status:
            return None
        try:
            automation = json.loads(self.server.worker_status.read_text(encoding="utf-8"))
            if not isinstance(automation, dict):
                return {"state": "unknown"}
            full_fetch_finished_at = automation.get("full_fetch_finished_at")
            if full_fetch_finished_at:
                try:
                    finished = datetime.fromisoformat(
                        str(full_fetch_finished_at).replace("Z", "+00:00")
                    )
                    if finished.tzinfo is None:
                        finished = finished.replace(tzinfo=timezone.utc)
                    automation["full_fetch_finished_seconds_ago"] = max(
                        0, int((datetime.now(timezone.utc) - finished).total_seconds())
                    )
                except ValueError:
                    pass
            return automation
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"state": "unknown"}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "invalid or missing token")
            return

        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/v1/status":
            publications = self.server.index.publications()
            payload: dict[str, Any] = {
                "name": "PressReader Sync Bridge",
                "version": 1,
                "publication_count": len(publications),
                "issue_count": sum(item.issue_count for item in publications),
            }
            automation = self._automation_status()
            if automation is not None:
                payload["automation"] = automation
            self._json(HTTPStatus.OK, payload)
            return
        if path == "/v1/publications":
            self._json(HTTPStatus.OK, {
                "publications": [asdict(item) for item in self.server.index.publications()]
            })
            return
        if path.startswith("/v1/publications/") and path.endswith("/issues"):
            publication_id = unquote(path[len("/v1/publications/"):-len("/issues")]).strip("/")
            issues = self.server.index.issues(publication_id)
            if issues is None:
                self._error(HTTPStatus.NOT_FOUND, "publication not found")
                return
            self._json(HTTPStatus.OK, {"issues": [item.public() for item in issues]})
            return
        if path == "/v1/latest":
            publication_id = parse_qs(parsed.query).get("publication", [""])[0]
            issues = self.server.index.issues(publication_id)
            if not issues:
                self._error(HTTPStatus.NOT_FOUND, "publication or edition not found")
                return
            self._json(HTTPStatus.OK, {"issue": issues[0].public()})
            return
        if path.startswith("/v1/files/"):
            issue_id = unquote(path[len("/v1/files/"):])
            issue = self.server.index.issue(issue_id)
            if issue is None:
                self._error(HTTPStatus.NOT_FOUND, "edition not found")
                return
            # The path was produced by the index, but verify containment again at serve time.
            try:
                issue.path.relative_to(self.server.index.root)
            except ValueError:
                self._error(HTTPStatus.FORBIDDEN, "invalid edition path")
                return
            try:
                size = issue.path.stat().st_size
                content_type = mimetypes.guess_type(issue.filename)[0] or "application/octet-stream"
                self._headers(HTTPStatus.OK, content_type, size)
                with issue.path.open("rb") as handle:
                    while chunk := handle.read(1024 * 256):
                        self.wfile.write(chunk)
            except (OSError, BrokenPipeError, ConnectionResetError):
                return
            return
        self._error(HTTPStatus.NOT_FOUND, "endpoint not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "invalid or missing token")
            return

        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path != "/v1/automation/run":
            self._error(HTTPStatus.NOT_FOUND, "endpoint not found")
            return
        if not self.server.worker_trigger:
            self._error(HTTPStatus.NOT_IMPLEMENTED, "automation trigger is not configured")
            return

        automation = self._automation_status() or {}
        if automation.get("state") == "running":
            self._json(HTTPStatus.OK, {"accepted": False, "state": "running"})
            return

        try:
            self.server.worker_trigger.parent.mkdir(parents=True, exist_ok=True)
            self.server.worker_trigger.touch(exist_ok=True)
        except OSError as err:
            self.log_error("could not request worker run: %s", err)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "could not request automation run")
            return
        self._json(HTTPStatus.ACCEPTED, {
            "accepted": True,
            "state": "queued",
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })


def lan_addresses(port: int) -> list[str]:
    addresses = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    return [f"http://{address}:{port}" for address in sorted(addresses)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True, help="publication library folder")
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8787, help="listen port (default: 8787)")
    parser.add_argument(
        "--token",
        default=os.environ.get(
            "PRESSREADER_SYNC_TOKEN",
            os.environ.get("PRESSREADER_SHELF_TOKEN", os.environ.get("PRESSKO_TOKEN", "")),
        ),
        help="bearer token (legacy PRESSREADER_SHELF_TOKEN and PRESSKO_TOKEN are also accepted)",
    )
    parser.add_argument("--token-file", type=Path, help="read bearer token from a private file")
    parser.add_argument("--cache-seconds", type=float, default=5.0, help="index cache lifetime")
    parser.add_argument("--worker-status", type=Path, help="optional worker status JSON file")
    parser.add_argument("--worker-trigger", type=Path, help="file used to request an immediate worker run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.library.is_dir():
        print(f"error: library folder does not exist: {args.library}", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("error: port must be between 1 and 65535", file=sys.stderr)
        return 2
    token = args.token
    if args.token_file:
        try:
            token = args.token_file.read_text(encoding="utf-8").strip()
        except OSError as err:
            print(f"error: could not read token file: {err}", file=sys.stderr)
            return 2
    index = LibraryIndex(args.library, max(0.0, args.cache_seconds))
    index.rebuild()
    server = PressReaderSyncServer(
        (args.host, args.port), index, token, args.worker_status, args.worker_trigger
    )
    print(f"PressReader Sync Bridge: {len(index.publications())} publication(s)")
    for address in lan_addresses(server.server_address[1]):
        print(f"  {address}")
    if not token:
        print("WARNING: no token configured; every device on this network can download the library", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run PressReader Sync for a PressReader Sync library."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from epub_cleaner import clean_pressreader_epub

LOG = logging.getLogger("pressreader_sync.worker")
PRESSREADER_HOME = "https://www.pressreader.com/"
DEFAULT_CATALOG_URL = "https://www.pressreader.com/catalog"
MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
ISSUE_DATE_RE = re.compile(rf"\b(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})\b", re.IGNORECASE)
STOP = False


def stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def safe_component(value: str, fallback: str = "Publication") -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    return (value[:120].rstrip() or fallback)


def normalized_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def parse_issue_date(text: str) -> str:
    match = ISSUE_DATE_RE.search(text or "")
    if not match:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        return datetime.strptime(match.group(1).title(), "%d %b %Y").date().isoformat()
    except ValueError:
        return datetime.now(timezone.utc).date().isoformat()


def is_epub(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read("mimetype").strip() == b"application/epub+zip"
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


@dataclass(frozen=True)
class PublicationLink:
    title: str
    url: str


@dataclass
class RunStatus:
    state: str = "starting"
    started_at: str = ""
    finished_at: str = ""
    next_run_at: str = ""
    discovered: int = 0
    exported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.ledger_path = directory / "exports.json"
        self.status_path = directory / "worker-status.json"
        directory.mkdir(parents=True, exist_ok=True)
        self.ledger = self._load(self.ledger_path, {})

    @staticmethod
    def _load(path: Path, fallback: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def already_exported(self, url: str, issue_date: str) -> bool:
        return self.ledger.get(normalized_url(url), {}).get("issue_date") == issue_date

    def mark_exported(self, publication: PublicationLink, issue_date: str, file_path: Path) -> None:
        self.ledger[normalized_url(publication.url)] = {
            "title": publication.title,
            "issue_date": issue_date,
            "file": str(file_path),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_json(self.ledger_path, self.ledger)

    def write_status(self, status: RunStatus) -> None:
        self._atomic_json(self.status_path, asdict(status))


class PressReaderAutomation:
    def __init__(self, page: Page, library: Path, state: StateStore, diagnostics: Path):
        self.page = page
        self.library = library
        self.state = state
        self.diagnostics = diagnostics
        self.page.set_default_timeout(15_000)

    def save_diagnostic(self, name: str) -> None:
        self.diagnostics.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = self.diagnostics / f"{stamp}-{safe_component(name, 'diagnostic')}"
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            base.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
        except Exception as err:  # diagnostics must never hide the original failure
            LOG.warning("Could not save browser diagnostic: %s", err)

    def discover_my_publications(self, catalog_url: str) -> list[PublicationLink]:
        LOG.info("Opening PressReader catalog")
        self.page.goto(catalog_url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(5_000)
        grid = self.page.locator('[role="group"][aria-label="My Publications"]').first
        try:
            grid.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError as err:
            self.save_diagnostic("my-publications-not-found")
            if self.page.get_by_text(re.compile(r"sign in|log in", re.I)).count():
                raise RuntimeError("PressReader login has expired; run the login profile again") from err
            raise RuntimeError("Could not find My Publications; see the diagnostics folder") from err

        raw_links = grid.locator('a[data-testid^="publication-"][href]').evaluate_all("""links =>
            links.map(a => ({
                url: a.href,
                title: (a.querySelector('.page-source')?.innerText ||
                        [...a.querySelectorAll('.sr-only')].map(x => x.innerText).find(x => x && !/^(Magazine|Newspaper)$/i.test(x)) ||
                        a.querySelector('img')?.alt || a.getAttribute('title') || '').trim()
            }))""")
        excluded = ("/catalog", "/search", "/account", "/help", "/settings")
        found: dict[str, PublicationLink] = {}
        for item in raw_links:
            url = urljoin(PRESSREADER_HOME, str(item.get("url", "")))
            parsed = urlsplit(url)
            if not parsed.netloc.endswith("pressreader.com") or any(parsed.path.rstrip("/") == value for value in excluded):
                continue
            key = normalized_url(url)
            title = safe_component(str(item.get("title", "")).splitlines()[0], parsed.path.rsplit("/", 1)[-1])
            found[key] = PublicationLink(title=title, url=url)
        if not found:
            self.save_diagnostic("my-publications-empty")
            raise RuntimeError("My Publications was found but no publication links could be identified")
        publications = list(found.values())
        LOG.info("Discovered %d saved publication(s)", len(publications))
        return publications

    def _visible(self, locator: Locator) -> bool:
        try:
            return locator.count() > 0 and locator.first.is_visible()
        except Exception:
            return False

    def _open_publication_menu(self) -> None:
        export = self.page.get_by_text(re.compile(r"Export\s+to\s+eReader", re.I))
        if self._visible(export):
            return
        selectors = [
            os.environ.get("PRESSREADER_MORE_SELECTOR", ""),
            "button.btn-icon:has(> .pri-options)",
            ".site-header-v2 .dropdown.btn-options > button.btn-icon",
            ".dropdown.btn-options > button[aria-haspopup='true']",
            "button[aria-label*='more' i]",
            "button[title*='more' i]",
            "[role='button'][aria-label*='more' i]",
            "button[aria-label*='option' i]",
            "button[title*='option' i]",
            "button[class*='more' i]",
            "button[class*='menu' i]",
            "button[class*='ellipsis' i]",
            "button[class*='overflow' i]",
            "[data-testid*='more' i]",
        ]
        candidates: list[Locator] = []
        for selector in selectors:
            if not selector:
                continue
            locator = self.page.locator(selector)
            for index in range(min(locator.count(), 12)):
                candidates.append(locator.nth(index))
        for candidate in candidates:
            try:
                if not candidate.is_visible():
                    continue
                candidate.click(timeout=3_000)
                self.page.wait_for_timeout(500)
                if self._visible(export):
                    return
                if self.page.locator(".dropmenu-body-wrapper:visible").count():
                    self.save_diagnostic("publication-menu-open")
                self.page.keyboard.press("Escape")
            except Exception:
                continue
        self.save_diagnostic("export-menu-not-found")
        raise RuntimeError("Could not open the publication menu")

    def _enter_reader(self, publication_url: str) -> None:
        # Account/HotSpot welcome and cookie consent may cover the publication
        # detail page even in a previously authenticated browser profile.
        for label in ("Allow all", "Accept all"):
            button = self.page.get_by_text(label, exact=True)
            if self._visible(button):
                button.click()
                self.page.wait_for_timeout(500)
                break
        reconnect = self.page.get_by_text("Start reading now", exact=True)
        if self._visible(reconnect):
            # With active access the welcome dialog has a visually small Close
            # link. "Start reading now" instead returns to the catalog, which is
            # useful for renewal but wrong when we are already on a saved title.
            close_welcome = self.page.locator(".alert-hotspot .alert-close").first
            if self._visible(close_welcome):
                close_welcome.click()
            else:
                reconnect.click()
                self.page.wait_for_timeout(3_000)
                if normalized_url(self.page.url) != normalized_url(publication_url):
                    self.page.goto(publication_url, wait_until="domcontentloaded", timeout=60_000)
                    self.page.wait_for_timeout(3_000)
                close_welcome = self.page.locator(".alert-hotspot .alert-close").first
                if self._visible(close_welcome):
                    close_welcome.click()
            self.page.wait_for_timeout(1_000)

        read_now = self.page.locator('[data-testid="readNowButton"]').first
        if self._visible(read_now):
            read_now.click()
            self.page.wait_for_timeout(8_000)
            return
        # Some saved links may already point straight to the reader.
        if "/page/" in self.page.url or "/view/" in self.page.url:
            return
        self.save_diagnostic("read-button-not-found")
        raise RuntimeError("Could not enter the latest issue reader")

    def export_latest(self, publication: PublicationLink) -> str:
        LOG.info("Checking %s", publication.title)
        self.page.goto(publication.url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(5_000)
        self._enter_reader(publication.url)
        self._open_publication_menu()
        menu_text = self.page.locator("body").inner_text()
        issue_date = parse_issue_date(menu_text)
        if self.state.already_exported(publication.url, issue_date):
            LOG.info("Already have %s dated %s", publication.title, issue_date)
            self.page.keyboard.press("Escape")
            return "skipped"

        self.page.get_by_text(re.compile(r"Export\s+to\s+eReader", re.I)).click()
        export_device = os.environ.get("PRESSREADER_SYNC_EXPORT_DEVICE", "Nook")
        device = self.page.get_by_text(export_device, exact=True)
        device.wait_for(state="visible")
        device.click()
        done = self.page.get_by_text("Done", exact=True)
        downloads: list[Any] = []

        def capture_download(download: Any) -> None:
            downloads.append(download)

        self.page.on("download", capture_download)
        try:
            done.click()
            deadline = time.monotonic() + 180
            pressreader_error = self.page.get_by_text(re.compile(r"Something\s+went\s+wrong", re.I))
            while not downloads:
                if self._visible(pressreader_error):
                    raise RuntimeError(f"PressReader reported that export is unavailable for {publication.title}")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"PressReader did not deliver the export for {publication.title} within 180 seconds")
                self.page.wait_for_timeout(500)
        finally:
            self.page.remove_listener("download", capture_download)
        download = downloads[0]
        suggested = download.suggested_filename
        temporary = self.state.directory / ("incoming-" + safe_component(suggested, "issue.epub"))
        download.save_as(str(temporary))
        if not is_epub(temporary):
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"{export_device} export for {publication.title} was not a standard EPUB")

        cleanup = clean_pressreader_epub(temporary)
        LOG.info(
            "Cleaned %s: %d/%d articles kept, %d duplicates and %d page assets removed, %.1f%% smaller",
            publication.title,
            cleanup.articles_kept,
            cleanup.articles_found,
            cleanup.duplicates_removed,
            cleanup.assets_removed,
            100 * (cleanup.original_bytes - cleanup.cleaned_bytes) / cleanup.original_bytes,
        )

        publication_dir = self.library / safe_component(publication.title)
        publication_dir.mkdir(parents=True, exist_ok=True)
        metadata = publication_dir / "publication.json"
        if not metadata.exists():
            metadata.write_text(json.dumps({
                "title": publication.title,
                "source_url": publication.url,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        destination = publication_dir / f"{issue_date} - {safe_component(publication.title)}.epub"
        temporary.replace(destination)
        self.state.mark_exported(publication, issue_date, destination)
        LOG.info("Exported %s", destination)
        return "exported"


def launch_context(playwright: Any, profile: Path, headless: bool) -> BrowserContext:
    profile.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1440, "height": 1000},
        locale=os.environ.get(
            "PRESSREADER_SYNC_LOCALE",
            os.environ.get("PRESSREADER_SHELF_LOCALE", os.environ.get("PRESSKO_LOCALE", "en-US")),
        ),
        timezone_id=os.environ.get(
            "PRESSREADER_SYNC_TIMEZONE",
            os.environ.get("PRESSREADER_SHELF_TIMEZONE", os.environ.get("PRESSKO_TIMEZONE", "Europe/Lisbon")),
        ),
        args=["--disable-dev-shm-usage"],
    )


def interactive_login(profile: Path) -> int:
    LOG.info("Opening PressReader. Sign in, verify My Publications, then close the browser tab.")
    with sync_playwright() as playwright:
        context = launch_context(playwright, profile, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(DEFAULT_CATALOG_URL, wait_until="domcontentloaded", timeout=60_000)
        while not STOP and len(context.pages) > 0:
            page.wait_for_timeout(1_000)
        context.close()
    return 0


def run_once(
    profile: Path,
    library: Path,
    state_dir: Path,
    diagnostics: Path,
    catalog_url: str,
    limit: int = 0,
) -> RunStatus:
    state = StateStore(state_dir)
    status = RunStatus(state="running", started_at=datetime.now(timezone.utc).isoformat())
    state.write_status(status)
    with sync_playwright() as playwright:
        context = launch_context(playwright, profile, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            automation = PressReaderAutomation(page, library, state, diagnostics)
            publications = automation.discover_my_publications(catalog_url)
            status.discovered = len(publications)
            if limit > 0:
                publications = publications[:limit]
            for publication in publications:
                if STOP:
                    break
                try:
                    result = automation.export_latest(publication)
                    if result == "exported":
                        status.exported += 1
                    else:
                        status.skipped += 1
                except Exception as err:
                    LOG.exception("Failed to export %s", publication.title)
                    status.errors.append(f"{publication.title}: {err}")
                    automation.save_diagnostic("export-failed-" + publication.title)
        except Exception as err:
            LOG.exception("PressReader synchronization failed")
            status.errors.append(str(err))
        finally:
            context.close()
    status.finished_at = datetime.now(timezone.utc).isoformat()
    status.state = "error" if status.errors else "ok"
    state.write_status(status)
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("login", "once", "run"))
    parser.add_argument("--profile", type=Path, default=Path("/data/browser"))
    parser.add_argument("--library", type=Path, default=Path("/library"))
    parser.add_argument("--state", type=Path, default=Path("/state"))
    parser.add_argument("--diagnostics", type=Path, default=Path("/state/diagnostics"))
    parser.add_argument("--catalog-url", default=os.environ.get("PRESSREADER_CATALOG_URL", DEFAULT_CATALOG_URL))
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get(
            "PRESSREADER_SYNC_INTERVAL_SECONDS",
            os.environ.get("PRESSREADER_SHELF_INTERVAL_SECONDS", os.environ.get("PRESSKO_INTERVAL_SECONDS", "21600")),
        )),
    )
    parser.add_argument("--limit", type=int, default=0, help="maximum publications per run (0 means all)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    args = parse_args(argv)
    if args.command == "login":
        return interactive_login(args.profile)
    if args.command == "once":
        status = run_once(args.profile, args.library, args.state, args.diagnostics, args.catalog_url, args.limit)
        return 1 if status.errors else 0
    interval = max(300, args.interval)
    while not STOP:
        started = time.monotonic()
        status = run_once(args.profile, args.library, args.state, args.diagnostics, args.catalog_url, args.limit)
        remaining = max(0, interval - int(time.monotonic() - started))
        status.next_run_at = datetime.fromtimestamp(time.time() + remaining, timezone.utc).isoformat()
        StateStore(args.state).write_status(status)
        LOG.info("Next check in %d seconds", remaining)
        for _ in range(remaining):
            if STOP:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

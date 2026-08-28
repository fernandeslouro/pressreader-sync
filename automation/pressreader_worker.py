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

from epub_cleaner import clean_pressreader_epub, reader_style_is_current, style_pressreader_epub

LOG = logging.getLogger("pressreader_sync.worker")
PRESSREADER_HOME = "https://www.pressreader.com/"
DEFAULT_CATALOG_URL = "https://www.pressreader.com/catalog"
MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
ISSUE_DATE_RE = re.compile(rf"\b(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})\b", re.IGNORECASE)
STOP = False
DEFAULT_RETRY_DELAYS = (600, 1800, 3600, 10800)


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


def parse_issue_date(text: str) -> str | None:
    match = ISSUE_DATE_RE.search(text or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).title(), "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def retry_delay_seconds(failure_count: int, delays: tuple[int, ...] = DEFAULT_RETRY_DELAYS) -> int:
    """Return the retry delay, keeping the final delay for later failures."""
    if not delays:
        return 10800
    index = min(max(1, failure_count), len(delays)) - 1
    return max(1, delays[index])


def export_devices() -> list[str]:
    configured = os.environ.get("PRESSREADER_SYNC_EXPORT_DEVICES", "").strip()
    if not configured:
        preferred = os.environ.get("PRESSREADER_SYNC_EXPORT_DEVICE", "Nook").strip() or "Nook"
        configured = ",".join((preferred, "Kobo", "Sony"))
    devices: list[str] = []
    for value in configured.split(","):
        value = value.strip()
        if value and value.casefold() not in {item.casefold() for item in devices}:
            devices.append(value)
    return devices or ["Nook"]


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
    attempted: int = 0
    exported: int = 0
    skipped: int = 0
    deferred: int = 0
    errors: list[str] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.ledger_path = directory / "exports.json"
        self.retry_path = directory / "retries.json"
        self.status_path = directory / "worker-status.json"
        directory.mkdir(parents=True, exist_ok=True)
        self.ledger = self._load(self.ledger_path, {})
        self.retries = self._load(self.retry_path, {})
        if not isinstance(self.retries, dict):
            self.retries = {}

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
        self.clear_failure(publication.url)

    def retry_due(self, url: str, now: float | None = None) -> bool:
        retry = self.retries.get(normalized_url(url))
        if not isinstance(retry, dict):
            return False
        now = time.time() if now is None else now
        return float(retry.get("next_retry_timestamp", 0)) <= now

    def record_failure(
        self,
        publication: PublicationLink,
        error: str,
        issue_date: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        key = normalized_url(publication.url)
        previous = self.retries.get(key, {})
        same_issue = (
            isinstance(previous, dict)
            and (not issue_date or previous.get("issue_date") in ("", issue_date))
        )
        failures = int(previous.get("failures", 0)) + 1 if same_issue else 1
        delay = retry_delay_seconds(failures)
        next_retry = now + delay
        entry = {
            "title": publication.title,
            "issue_date": issue_date,
            "failures": failures,
            "last_error": error,
            "failed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "next_retry_at": datetime.fromtimestamp(next_retry, timezone.utc).isoformat(),
            "next_retry_timestamp": next_retry,
        }
        self.retries[key] = entry
        self._atomic_json(self.retry_path, self.retries)
        return entry

    def clear_failure(self, url: str) -> None:
        if self.retries.pop(normalized_url(url), None) is not None:
            self._atomic_json(self.retry_path, self.retries)

    def next_retry_timestamp(self) -> float | None:
        timestamps = [
            float(item.get("next_retry_timestamp", 0))
            for item in self.retries.values()
            if isinstance(item, dict) and item.get("next_retry_timestamp")
        ]
        return min(timestamps) if timestamps else None

    def defer_overdue_retries(self, now: float | None = None, delay: int = 600) -> None:
        """Prevent a missing publication from causing a tight retry loop."""
        now = time.time() if now is None else now
        changed = False
        for item in self.retries.values():
            if not isinstance(item, dict) or float(item.get("next_retry_timestamp", 0)) > now:
                continue
            next_retry = now + delay
            item["next_retry_timestamp"] = next_retry
            item["next_retry_at"] = datetime.fromtimestamp(next_retry, timezone.utc).isoformat()
            changed = True
        if changed:
            self._atomic_json(self.retry_path, self.retries)

    def public_retries(self) -> list[dict[str, Any]]:
        retries = []
        for item in self.retries.values():
            if not isinstance(item, dict):
                continue
            retries.append({
                key: item.get(key)
                for key in ("title", "issue_date", "failures", "last_error", "failed_at", "next_retry_at")
            })
        return sorted(retries, key=lambda item: str(item.get("next_retry_at", "")))

    def write_status(self, status: RunStatus) -> None:
        self._atomic_json(self.status_path, asdict(status))


class PressReaderAutomation:
    def __init__(self, page: Page, library: Path, state: StateStore, diagnostics: Path):
        self.page = page
        self.library = library
        self.state = state
        self.diagnostics = diagnostics
        self.current_issue_date = ""
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

        excluded = ("/catalog", "/search", "/account", "/help", "/settings")
        found: dict[str, PublicationLink] = {}

        # The shelf is a horizontally virtualized React grid: only the cards in
        # and near the viewport exist in the DOM.  Read each viewport, then use
        # the shelf's own paddle to make the remaining saved titles mount.
        for _ in range(100):
            raw_links = grid.locator('a[data-testid^="publication-"][href]').evaluate_all("""links =>
                links.map(a => ({
                    url: a.href,
                    title: (a.querySelector('.page-source')?.innerText ||
                            [...a.querySelectorAll('.sr-only')].map(x => x.innerText).find(x => x && !/^(Magazine|Newspaper)$/i.test(x)) ||
                            a.querySelector('img')?.alt || a.getAttribute('title') || '').trim()
                }))""")
            for item in raw_links:
                url = urljoin(PRESSREADER_HOME, str(item.get("url", "")))
                parsed = urlsplit(url)
                if not parsed.netloc.endswith("pressreader.com") or any(
                    parsed.path.rstrip("/") == value for value in excluded
                ):
                    continue
                key = normalized_url(url)
                title = safe_component(
                    str(item.get("title", "")).splitlines()[0],
                    parsed.path.rsplit("/", 1)[-1],
                )
                found[key] = PublicationLink(title=title, url=url)

            metrics = grid.evaluate("""el => ({
                left: el.scrollLeft,
                viewport: el.clientWidth,
                width: el.scrollWidth
            })""")
            if metrics["left"] + metrics["viewport"] >= metrics["width"] - 2:
                break
            previous_left = metrics["left"]
            grid.evaluate("""el => {
                el.scrollLeft = Math.min(
                    el.scrollWidth,
                    el.scrollLeft + Math.max(300, el.clientWidth * 0.8)
                );
                el.dispatchEvent(new Event('scroll', {bubbles: true}));
            }""")
            self.page.wait_for_timeout(500)
            if grid.evaluate("el => el.scrollLeft") <= previous_left + 1:
                break
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

    def _open_latest_export_menu(self, publication: PublicationLink) -> str:
        self.page.goto(publication.url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(5_000)
        self._enter_reader(publication.url)
        self._open_publication_menu()
        menu_text = self.page.locator("body").inner_text()
        issue_date = parse_issue_date(menu_text)
        if not issue_date:
            self.save_diagnostic("issue-date-not-found-" + publication.title)
            raise RuntimeError(
                f"Could not verify the issue date for {publication.title}; refusing to guess"
            )
        self.current_issue_date = issue_date
        return issue_date

    def _download_export(self, publication: PublicationLink, device_name: str) -> Path:
        self.page.get_by_text(re.compile(r"Export\s+to\s+eReader", re.I)).click()
        device = self.page.get_by_text(device_name, exact=True)
        try:
            device.wait_for(state="visible", timeout=5_000)
        except PlaywrightTimeoutError as err:
            raise RuntimeError(f"{device_name} is not offered") from err
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
                    raise RuntimeError(f"PressReader rejected the {device_name} export")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"PressReader did not deliver the {device_name} export within 180 seconds")
                self.page.wait_for_timeout(500)
        finally:
            self.page.remove_listener("download", capture_download)
        download = downloads[0]
        suggested = download.suggested_filename
        temporary = self.state.directory / (
            "incoming-" + safe_component(device_name, "device") + "-" + safe_component(suggested, "issue.epub")
        )
        download.save_as(str(temporary))
        if not is_epub(temporary):
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"{device_name} export was not a standard EPUB")
        return temporary

    def export_latest(self, publication: PublicationLink) -> str:
        LOG.info("Checking %s", publication.title)
        self.current_issue_date = ""
        issue_date = self._open_latest_export_menu(publication)
        if self.state.already_exported(publication.url, issue_date):
            record = self.state.ledger.get(normalized_url(publication.url), {})
            exported_file = Path(str(record.get("file", "")))
            if exported_file.is_file() and not reader_style_is_current(exported_file):
                style_pressreader_epub(exported_file)
                LOG.info("Updated reader styling for %s dated %s", publication.title, issue_date)
            LOG.info("Already have %s dated %s", publication.title, issue_date)
            self.page.keyboard.press("Escape")
            self.state.clear_failure(publication.url)
            return "skipped"

        temporary: Path | None = None
        failures: list[str] = []
        devices = export_devices()
        for index, device_name in enumerate(devices):
            if index:
                retry_date = self._open_latest_export_menu(publication)
                if retry_date != issue_date:
                    raise RuntimeError(
                        f"Issue changed from {issue_date} to {retry_date} while trying export formats"
                    )
            try:
                temporary = self._download_export(publication, device_name)
                LOG.info("Downloaded %s using the %s export", publication.title, device_name)
                break
            except Exception as err:
                failures.append(f"{device_name}: {err}")
                LOG.warning("%s export failed for %s: %s", device_name, publication.title, err)
        if temporary is None:
            raise RuntimeError("all configured exports failed (" + "; ".join(failures) + ")")

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


def launch_context(
    playwright: Any,
    profile: Path,
    headless: bool,
    proxy_server: str = "",
) -> BrowserContext:
    profile.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "user_data_dir": str(profile),
        "headless": headless,
        "accept_downloads": True,
        "viewport": {"width": 1440, "height": 1000},
        "locale": os.environ.get(
            "PRESSREADER_SYNC_LOCALE",
            os.environ.get("PRESSREADER_SHELF_LOCALE", os.environ.get("PRESSKO_LOCALE", "en-US")),
        ),
        "timezone_id": os.environ.get(
            "PRESSREADER_SYNC_TIMEZONE",
            os.environ.get("PRESSREADER_SHELF_TIMEZONE", os.environ.get("PRESSKO_TIMEZONE", "Europe/Lisbon")),
        ),
        "args": ["--disable-dev-shm-usage"],
    }
    proxy_server = proxy_server.strip() or os.environ.get("PRESSREADER_SYNC_PROXY", "").strip()
    if proxy_server:
        options["proxy"] = {"server": proxy_server}
    return playwright.chromium.launch_persistent_context(**options)


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
    proxy_server: str = "",
    only_title: str = "",
    exclude_title: str = "",
    retry_only: bool = False,
) -> RunStatus:
    state = StateStore(state_dir)
    status = RunStatus(state="running", started_at=datetime.now(timezone.utc).isoformat())
    state.write_status(status)
    with sync_playwright() as playwright:
        context = launch_context(playwright, profile, headless=True, proxy_server=proxy_server)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            automation = PressReaderAutomation(page, library, state, diagnostics)
            publications = automation.discover_my_publications(catalog_url)
            status.discovered = len(publications)
            if only_title:
                publications = [
                    publication
                    for publication in publications
                    if publication.title.casefold() == only_title.casefold()
                ]
            if exclude_title:
                publications = [
                    publication
                    for publication in publications
                    if publication.title.casefold() != exclude_title.casefold()
                ]
            if retry_only:
                before = len(publications)
                publications = [publication for publication in publications if state.retry_due(publication.url)]
                status.deferred = before - len(publications)
            if limit > 0:
                publications = publications[:limit]
            status.attempted = len(publications)
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
                    message = f"{publication.title}: {err}"
                    status.errors.append(message)
                    retry = state.record_failure(
                        publication,
                        str(err),
                        issue_date=automation.current_issue_date,
                    )
                    LOG.info(
                        "Will retry %s at %s after failure %d",
                        publication.title,
                        retry["next_retry_at"],
                        retry["failures"],
                    )
                    automation.save_diagnostic("export-failed-" + publication.title)
        except Exception as err:
            LOG.exception("PressReader synchronization failed")
            status.errors.append(str(err))
        finally:
            context.close()
    status.finished_at = datetime.now(timezone.utc).isoformat()
    status.state = "partial" if status.errors and (status.exported or status.skipped) else (
        "error" if status.errors else "ok"
    )
    status.retries = StateStore(state_dir).public_retries()
    state.write_status(status)
    return status


def run_cycle(
    profile: Path,
    library: Path,
    state_dir: Path,
    diagnostics: Path,
    catalog_url: str,
    limit: int = 0,
    retry_only: bool = False,
) -> RunStatus:
    special_proxy = os.environ.get("PRESSREADER_SYNC_SPECIAL_PROXY", "").strip()
    special_title = os.environ.get("PRESSREADER_SYNC_SPECIAL_TITLE", "").strip()
    if not special_proxy or not special_title:
        return run_once(
            profile, library, state_dir, diagnostics, catalog_url, limit,
            retry_only=retry_only,
        )

    direct = run_once(
        profile,
        library,
        state_dir,
        diagnostics,
        catalog_url,
        limit,
        exclude_title=special_title,
        retry_only=retry_only,
    )
    special = run_once(
        profile,
        library,
        state_dir,
        diagnostics,
        catalog_url,
        limit,
        proxy_server=special_proxy,
        only_title=special_title,
        retry_only=retry_only,
    )
    combined = RunStatus(
        state="partial" if (direct.errors or special.errors) and (
            direct.exported or direct.skipped or special.exported or special.skipped
        ) else ("error" if direct.errors or special.errors else "ok"),
        started_at=direct.started_at,
        finished_at=special.finished_at,
        discovered=direct.discovered + special.discovered,
        attempted=direct.attempted + special.attempted,
        exported=direct.exported + special.exported,
        skipped=direct.skipped + special.skipped,
        deferred=direct.deferred + special.deferred,
        errors=direct.errors + special.errors,
        retries=StateStore(state_dir).public_retries(),
    )
    StateStore(state_dir).write_status(combined)
    return combined


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
        status = run_cycle(args.profile, args.library, args.state, args.diagnostics, args.catalog_url, args.limit)
        return 1 if status.errors else 0
    interval = max(300, args.interval)
    next_regular_run = 0.0
    while not STOP:
        cycle_started = time.time()
        retry_only = cycle_started < next_regular_run
        status = run_cycle(
            args.profile, args.library, args.state, args.diagnostics,
            args.catalog_url, args.limit, retry_only=retry_only,
        )
        if not retry_only:
            next_regular_run = cycle_started + interval
        state = StateStore(args.state)
        state.defer_overdue_retries()
        retry_at = state.next_retry_timestamp()
        next_run = min(next_regular_run, retry_at) if retry_at else next_regular_run
        remaining = max(1, int(next_run - time.time()))
        status.retries = state.public_retries()
        status.next_run_at = datetime.fromtimestamp(next_run, timezone.utc).isoformat()
        state.write_status(status)
        LOG.info("Next check in %d seconds", remaining)
        for _ in range(remaining):
            if STOP:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

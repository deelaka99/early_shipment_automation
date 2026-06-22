"""
Stage 02 – Shipping Report Generation
======================================
Logs into Helm (DC), triggers a Shipping Report export, waits for it to
complete in Export History, then downloads the file to
downloads/dc_shipping_report.csv.

Although the process guide shows manual browser clicks for reference, this
stage automates the same sequence through Playwright so no manual interaction
is required.

PDF step mapping
----------------
Step 3  Select "Reports" from the sidebar          → open_reports_page()
Step 4  Click "Shipping" from the reports list     → open_shipping_reports_section()
Step 5  Click "Download Report"                    → request_shipping_report_export()
Step 6  Navigate to "History"                      → open_helm_export_history()
Step 7  Click the download icon in History         → download_completed_shipping_report_from_history()
Step 8  File saved to downloads/                   → _save_download() / _download_helm_export_url()

Required columns consumed by Stage 03
--------------------------------------
Order ID, Date Despatched, Consignment Number,
Cancelled, Shipping Service Booked, Courier.
Important: Consignment Number is the tracking number — not Print Ref.

Steps in this stage
-------------------
Step 2.1  Login to Helm
Step 2.2  Navigate to the Reports page
Step 2.3  Open the Shipping reports section
Step 2.4  Request a new Shipping Report export
Step 2.5  Navigate to Export History
Step 2.6  Poll until the report status is Completed
Step 2.7  Download the completed Shipping Report

Required .env keys
------------------
HELM_EMAIL     – Helm login email
HELM_PASSWORD  – Helm login password

Optional .env keys
------------------
HEADLESS                          – Run browser headlessly (default: false)
HELM_MANUAL_LOGIN_FALLBACK        – Allow manual login if credentials fail (default: true)
HELM_MANUAL_LOGIN_TIMEOUT_SECONDS – Seconds to wait for manual login (default: 300)
HELM_REPORT_READY_TIMEOUT_SECONDS – Max seconds to wait for report (default: 2400)
DEBUG                             – Set to 1 to enable verbose logging

Exit codes
----------
0  – Report downloaded successfully
1  – Any error
"""

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DOTENV_PATH = Path(__file__).resolve().with_name(".env")
OUTPUT_PATH = Path("downloads/dc_shipping_report.csv").resolve()
DOWNLOAD_DIR = Path("downloads").resolve()

HELM_URL = "https://mybeautyandcareltd1.myhelm.app/login.php?type=standard"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _log(message: str) -> None:
    print(f"[DONE] {message}")


def _log_wait(message: str) -> None:
    print(f"[WAIT] {message}")


def _log_info(debug: bool, message: str) -> None:
    if debug:
        print(f"[INFO] {message}")


def _wait_for_network_idle(page: Page, timeout_ms: int = 10000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


def _click_first_visible(
    locators: Sequence[Locator],
    description: str,
    timeout_ms: int = 5000,
) -> None:
    last_error: Optional[Exception] = None
    for locator in locators:
        try:
            locator.first.wait_for(state="visible", timeout=timeout_ms)
            locator.first.click(timeout=timeout_ms)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error
            continue
    raise RuntimeError(f"Could not click {description}.") from last_error


def _fill_first_visible(
    locators: Sequence[Locator],
    value: str,
    description: str,
    timeout_ms: int = 5000,
) -> Locator:
    last_error: Optional[Exception] = None
    for locator in locators:
        try:
            field = locator.first
            field.wait_for(state="visible", timeout=timeout_ms)
            field.click(timeout=timeout_ms)
            field.fill(value, timeout=timeout_ms)
            field.dispatch_event("input", timeout=timeout_ms)
            field.dispatch_event("change", timeout=timeout_ms)
            actual_value = field.input_value(timeout=timeout_ms)
            if actual_value != value:
                field.click(timeout=timeout_ms)
                field.press("Control+A", timeout=timeout_ms)
                field.type(value, delay=25, timeout=timeout_ms)
                field.dispatch_event("input", timeout=timeout_ms)
                field.dispatch_event("change", timeout=timeout_ms)
                actual_value = field.input_value(timeout=timeout_ms)
            if actual_value != value:
                raise RuntimeError(
                    f"Filled {description}, but the field value did not match."
                )
            return field
        except PlaywrightTimeoutError as error:
            last_error = error
            continue
    raise RuntimeError(f"Could not find {description}.") from last_error


def _origin_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    helm_url: str
    download_dir: Path
    output_path: Path
    helm_report_ready_timeout_seconds: int
    helm_manual_login_fallback: bool
    helm_manual_login_timeout_seconds: int
    headless: bool
    debug: bool

    @staticmethod
    def load(dotenv_path: Path = DOTENV_PATH) -> "Config":
        load_dotenv(dotenv_path=dotenv_path, override=True, encoding="utf-8-sig")

        return Config(
            email=_require_env("HELM_EMAIL").strip(),
            password=_require_env("HELM_PASSWORD"),
            helm_url=HELM_URL,
            download_dir=DOWNLOAD_DIR,
            output_path=OUTPUT_PATH,
            helm_report_ready_timeout_seconds=int(
                os.getenv("HELM_REPORT_READY_TIMEOUT_SECONDS") or "2400"
            ),
            helm_manual_login_fallback=_env_flag(
                "HELM_MANUAL_LOGIN_FALLBACK", default=True
            ),
            helm_manual_login_timeout_seconds=int(
                os.getenv("HELM_MANUAL_LOGIN_TIMEOUT_SECONDS") or "300"
            ),
            headless=_env_flag(
                "AUTOMATION_HEADLESS", default=_env_flag("HEADLESS", default=False)
            ),
            debug=_env_flag("DEBUG", default=False),
        )


# ---------------------------------------------------------------------------
# Stage 2 / Step 2.1 – Helm login
# ---------------------------------------------------------------------------


class LoginFlow:
    def __init__(self, page: Page, config: Config):
        self.page = page
        self.config = config

    def open(self) -> None:
        self.page.goto(self.config.helm_url, wait_until="load")
        _wait_for_network_idle(self.page)
        self.page.wait_for_timeout(1000)

    def fill_credentials(self) -> None:
        email_field = _fill_first_visible(
            [
                self.page.get_by_label("Email", exact=False),
                self.page.get_by_placeholder(re.compile("email", re.I)),
                self.page.locator("input[type='email']"),
                self.page.locator("input[name*='email' i]"),
            ],
            self.config.email,
            "email input",
        )
        password_field = _fill_first_visible(
            [
                self.page.get_by_label("Password", exact=False),
                self.page.get_by_placeholder(re.compile("password", re.I)),
                self.page.locator("input[type='password']"),
                self.page.locator("input[name*='password' i]"),
            ],
            self.config.password,
            "password input",
        )
        _log_info(
            self.config.debug,
            "Helm login fields filled: "
            f"email={bool(email_field.input_value())}, "
            f"password={bool(password_field.input_value())}",
        )

    def submit(self) -> None:
        _wait_for_network_idle(self.page)
        _click_first_visible(
            [
                self.page.get_by_role("button", name=re.compile("log in|login", re.I)),
                self.page.get_by_role("button", name=re.compile("sign in", re.I)),
            ],
            "login button",
        )

    def _login_error_message(self) -> Optional[str]:
        candidates = [
            self.page.get_by_text(
                re.compile(
                    r"Login failed!\s*Unable to verify your login credentials\.?",
                    re.I,
                )
            ),
            self.page.get_by_text(re.compile(r"\bLogin failed\b", re.I)),
            self.page.get_by_text(
                re.compile(r"Unable to verify your login credentials", re.I)
            ),
            self.page.locator(".alert.alert-danger"),
            self.page.locator(".alert-danger"),
        ]
        for locator in candidates:
            try:
                if locator.count() > 0 and locator.first.is_visible():
                    text = locator.first.text_content() or ""
                    text = re.sub(r"\s+", " ", text).strip()
                    return text or "Login failed"
            except PlaywrightTimeoutError:
                continue
        return None

    def _app_is_visible(self) -> bool:
        app_chrome = self.page.locator(
            "div.sidebar, ul.acc-menu, nav[role='navigation']"
        )
        login_form = self.page.locator(
            "input[type='password'], input[name*='password' i]"
        )
        try:
            if app_chrome.count() > 0 and app_chrome.first.is_visible():
                return True
            if login_form.count() == 0:
                return True
        except PlaywrightTimeoutError:
            return False
        return False

    def _wait_for_manual_login(self, reason: str) -> None:
        if not self.config.helm_manual_login_fallback:
            raise SystemExit(
                f"Login failed: {reason}. Check HELM_EMAIL/HELM_PASSWORD in your .env file."
            )
        if self.config.headless:
            raise SystemExit(
                f"Login failed: {reason}. Manual login fallback is enabled, but HEADLESS=true."
            )
        timeout_ms = self.config.helm_manual_login_timeout_seconds * 1000
        print(
            "[ACTION] Helm rejected the automated login. Log in manually in the "
            "open browser window; automation will continue after login succeeds."
        )
        start = time.monotonic()
        while (time.monotonic() - start) * 1000 < timeout_ms:
            if self._app_is_visible():
                return
            self.page.wait_for_timeout(1000)
        raise SystemExit(
            "Timed out waiting for manual Helm login. Check the browser window and try again."
        )

    def verify(self, timeout_ms: int = 15000) -> None:
        self.page.wait_for_timeout(500)
        start = time.monotonic()
        while True:
            error_text = self._login_error_message()
            if error_text:
                try:
                    self.page.screenshot(path="login_failed.png", full_page=True)
                except Exception:
                    pass
                self._wait_for_manual_login(error_text)
                return
            if self._app_is_visible():
                return
            if (time.monotonic() - start) * 1000 >= timeout_ms:
                break
            self.page.wait_for_timeout(250)

        error_text = self._login_error_message()
        if error_text:
            try:
                self.page.screenshot(path="login_failed.png", full_page=True)
            except Exception:
                pass
            self._wait_for_manual_login(error_text)
            return

        raise SystemExit(
            "Login did not complete within the expected time. If credentials are "
            "correct, the site may require extra steps (e.g., CAPTCHA/2FA) or the "
            "page UI changed."
        )


# ---------------------------------------------------------------------------
# Stage 2 / Step 2.2 – Navigate to Reports page (PDF Step 3)
# ---------------------------------------------------------------------------


def open_reports_page(page: Page, config: Config) -> None:
    reports_path = "/reports-new/download"
    reports_url = f"{_origin_url(config.helm_url)}{reports_path}"

    _click_first_visible(
        [
            page.locator(f"a[href='{reports_path}']"),
            page.locator("li.active a[href='/reports-new/download']"),
            page.get_by_role("link", name="Reports"),
        ],
        "Reports sidebar link",
        timeout_ms=10000,
    )
    page.wait_for_url(f"**{reports_path}", timeout=15000)
    _wait_for_network_idle(page)

    if reports_path not in page.url:
        page.goto(reports_url, wait_until="domcontentloaded")
        _wait_for_network_idle(page)


# ---------------------------------------------------------------------------
# Stage 2 / Step 2.3 – Open Shipping section (PDF Step 4)
# ---------------------------------------------------------------------------


def open_shipping_reports_section(page: Page) -> None:
    _click_first_visible(
        [
            page.locator("ul.reports-nav a[data-section='shipping']"),
            page.locator("a[href='#shipping'][data-section='shipping']"),
            page.get_by_role("link", name="Shipping"),
        ],
        "Shipping reports section",
        timeout_ms=10000,
    )
    _wait_for_network_idle(page)


# ---------------------------------------------------------------------------
# Stage 2 / Step 2.4 – Request Shipping Report export (PDF Step 5)
# ---------------------------------------------------------------------------


def request_shipping_report_export(page: Page, config: Config) -> int:
    """Clicks 'Download Report' and returns the epoch-ms timestamp of the request."""
    reports_path = "/reports-new/download"
    if reports_path not in page.url:
        page.goto(
            f"{_origin_url(config.helm_url)}{reports_path}",
            wait_until="domcontentloaded",
        )
        _wait_for_network_idle(page)

    open_shipping_reports_section(page)
    _log("Stage 2 / Step 2.3: Opened Shipping reports section")

    try:
        page.locator("form[data-report-name='dc_shipping_report']").first.wait_for(
            state="visible",
            timeout=15000,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Could not find the Helm Shipping Report form after opening the "
            "Shipping reports section."
        ) from exc

    requested_after_ms = int(time.time() * 1000)
    if not _click_shipping_report_download_button(page):
        raise RuntimeError("Could not find the Shipping Report export request button.")

    _log("Stage 2 / Step 2.4: Clicked 'Download Report' button")
    page.wait_for_timeout(1500)
    _wait_for_network_idle(page)
    return requested_after_ms


def _click_shipping_report_download_button(page: Page) -> bool:
    """Finds and clicks the Download Report button inside the dc_shipping_report form."""
    return bool(page.evaluate("""() => {
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden'
                && style.display !== 'none'
                && rect.width > 0
                && rect.height > 0;
        };

        const textOf = el => [
            el.innerText,
            el.textContent,
            el.value,
            el.getAttribute('title'),
            el.getAttribute('aria-label'),
            el.getAttribute('data-report-name'),
            el.getAttribute('name')
        ].filter(Boolean).join(' ');

        const btnSelector =
            "input[name='create_report'], input[type='submit'], " +
            "button[type='submit'], button";

        const isDownloadControl = el =>
            el.getAttribute('name') === 'create_report'
            || /download\\s+report|create|request|export/i.test(textOf(el));

        const click = el => {
            el.scrollIntoView({block: 'center', inline: 'center'});
            el.click();
            return true;
        };

        const exactForm = document.querySelector(
            "form[data-report-name='dc_shipping_report']"
        );
        if (exactForm) {
            const btn = Array.from(exactForm.querySelectorAll(btnSelector))
                .find(el => isVisible(el) && isDownloadControl(el));
            if (btn) return click(btn);
        }

        for (const candidate of Array.from(document.querySelectorAll(btnSelector))
                .filter(el => isVisible(el) && isDownloadControl(el))) {
            let cur = candidate;
            let combined = '';
            for (let i = 0; cur && i < 8; i++) {
                combined += ' ' + textOf(cur);
                cur = cur.parentElement;
            }
            const reportName = candidate.closest('form')?.getAttribute('data-report-name') || '';
            const haystack = `${combined} ${reportName}`;
            if (/shipping\\s+report|dc_shipping_report/i.test(haystack)
                    && !/purchase\\s+order|dc_purchase|ioss\\s+report/i.test(haystack)) {
                return click(candidate);
            }
        }

        return false;
    }"""))


# ---------------------------------------------------------------------------
# Stage 2 / Step 2.5 – Navigate to Export History (PDF Step 6)
# ---------------------------------------------------------------------------


def open_helm_export_history(page: Page, config: Config) -> None:
    page.goto(
        f"{_origin_url(config.helm_url)}/reports-new/history",
        wait_until="domcontentloaded",
    )
    _wait_for_network_idle(page)
    _log("Stage 2 / Step 2.5: Navigated to Export History")


# ---------------------------------------------------------------------------
# Stage 2 / Steps 2.6–2.7 – Poll history and download (PDF Steps 7–8)
# ---------------------------------------------------------------------------


def download_shipping_report(page: Page, config: Config) -> Path:
    config.download_dir.mkdir(parents=True, exist_ok=True)
    _log("Stage 2 / Step 2.2: Navigating to Reports page")
    open_reports_page(page, config)
    _log("Stage 2 / Step 2.2: Reports page opened")

    requested_after_ms = request_shipping_report_export(page, config)
    open_helm_export_history(page, config)

    return _poll_and_download(page, config, requested_after_ms)


def _poll_and_download(
    page: Page,
    config: Config,
    requested_after_ms: int,
) -> Path:
    user_hint = _helm_user_hint(config.email)
    deadline = time.monotonic() + config.helm_report_ready_timeout_seconds
    last_status: str | None = None

    while time.monotonic() < deadline:
        status = _shipping_report_status(page, user_hint, requested_after_ms)
        normalized = (status or "not found").strip()
        remaining = max(0, int(deadline - time.monotonic()))
        elapsed = config.helm_report_ready_timeout_seconds - remaining

        if normalized != last_status or config.debug:
            _log_wait(
                f"Stage 2 / Step 2.6: Report status is '{normalized}'. "
                f"Waiting up to {remaining}s more (elapsed {elapsed}s)."
            )
            last_status = normalized

        if normalized.lower() in {"cancelled", "failed"}:
            raise RuntimeError(
                f"Helm Shipping Report export ended as '{normalized}'. "
                "Check Helm Export History and start a new run."
            )

        if status and status.lower() == "completed":
            _log("Stage 2 / Step 2.6: Report is Completed — downloading")

            download_url = _shipping_report_download_url(
                page, user_hint, requested_after_ms
            )
            if download_url:
                path = _download_via_url(page, download_url, config.download_dir)
                _log(f"Stage 2 / Step 2.7: Downloaded Shipping Report to {path}")
                return path

            with page.expect_download(timeout=60000) as dl_info:
                if not _click_shipping_report_download_icon(
                    page, user_hint, requested_after_ms
                ):
                    raise RuntimeError(
                        "Report is completed but no download link or button was found "
                        "in Export History."
                    )
            path = _save_download(dl_info.value, config.download_dir)
            _log(f"Stage 2 / Step 2.7: Downloaded Shipping Report to {path}")
            return path

        page.wait_for_timeout(10000)
        try:
            page.reload(wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            _log_wait("Stage 2 / Step 2.6: History page refresh timed out; retrying.")
        _wait_for_network_idle(page)

    raise RuntimeError(
        "Timed out waiting for the Helm Shipping Report export to complete."
    )


# ---------------------------------------------------------------------------
# History page helpers
# ---------------------------------------------------------------------------

_FIND_ROW_JS = """
function findRequestedHistoryRow(reportType, userHint, requestedAfterMs) {
    const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
    return rows.find(el => {
        const cells = Array.from(el.querySelectorAll('td'));
        if (cells.length < 8) return false;
        const type    = cells[1]?.innerText.trim() || '';
        const user    = (cells[2]?.innerText.trim() || '').toLowerCase();
        const started = cells[5]?.innerText.trim() || '';
        const created = cells[4]?.innerText.trim() || '';
        return new RegExp(`^${reportType}$`, 'i').test(type)
            && (!userHint || user.includes(userHint.toLowerCase()))
            && historyTimeIsAfter(started || created, requestedAfterMs);
    });
}

function historyTimeIsAfter(rawValue, requestedAfterMs) {
    const parsed = parseHistoryTime(rawValue);
    return parsed !== null && parsed >= requestedAfterMs - 120000;
}

function parseHistoryTime(rawValue) {
    const text = (rawValue || '').replace(/\\s+/g, ' ').trim();
    if (!text) return null;
    const native = Date.parse(text);
    if (!Number.isNaN(native)) return native;
    const m = text.match(
        /^(\\d{1,2})\\s+([A-Za-z]{3,})\\s+(\\d{4})\\s+(\\d{1,2}):(\\d{2})\\s*(AM|PM)?$/i
    );
    if (!m) return null;
    const months = {jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11};
    const month = months[m[2].slice(0, 3).toLowerCase()];
    if (month === undefined) return null;
    let hour = Number(m[4]);
    const ampm = (m[6] || '').toUpperCase();
    if (ampm === 'PM' && hour < 12) hour += 12;
    if (ampm === 'AM' && hour === 12) hour = 0;
    return new Date(Number(m[3]), month, Number(m[1]), hour, Number(m[5])).getTime();
}
"""

_DOWNLOAD_LINK_SELECTOR = (
    'td:last-child a[download][href*="dc_shipping_report.csv"], '
    'td:last-child a[href*="dc_shipping_report.csv"], '
    'td:last-child a[download][href*="/shipping-"]'
)


def _shipping_report_status(
    page: Page, user_hint: str, requested_after_ms: int
) -> str | None:
    return page.evaluate(
        f"""({{userHint, requestedAfterMs}}) => {{
            {_FIND_ROW_JS}
            const row = findRequestedHistoryRow('Shipping Report', userHint, requestedAfterMs);
            if (!row) return null;
            return Array.from(row.querySelectorAll('td'))[3]?.innerText.trim() || null;
        }}""",
        {"userHint": user_hint, "requestedAfterMs": requested_after_ms},
    )


def _shipping_report_download_url(
    page: Page, user_hint: str, requested_after_ms: int
) -> str | None:
    return page.evaluate(
        f"""({{userHint, requestedAfterMs}}) => {{
            {_FIND_ROW_JS}
            const row = findRequestedHistoryRow('Shipping Report', userHint, requestedAfterMs);
            if (!row) return null;
            const cells = Array.from(row.querySelectorAll('td'));
            if (!/^Completed$/i.test(cells[3]?.innerText.trim() || '')) return null;
            return row.querySelector('{_DOWNLOAD_LINK_SELECTOR}')?.href || null;
        }}""",
        {"userHint": user_hint, "requestedAfterMs": requested_after_ms},
    )


def _click_shipping_report_download_icon(
    page: Page, user_hint: str, requested_after_ms: int
) -> bool:
    return bool(
        page.evaluate(
            f"""({{userHint, requestedAfterMs}}) => {{
            {_FIND_ROW_JS}
            const row = findRequestedHistoryRow('Shipping Report', userHint, requestedAfterMs);
            if (!row) return false;
            const cells = Array.from(row.querySelectorAll('td'));
            if (!/^Completed$/i.test(cells[3]?.innerText.trim() || '')) return false;
            const link = row.querySelector('{_DOWNLOAD_LINK_SELECTOR}');
            if (!link) return false;
            link.scrollIntoView({{block: 'center', inline: 'center'}});
            link.click();
            return true;
        }}""",
            {"userHint": user_hint, "requestedAfterMs": requested_after_ms},
        )
    )


def _helm_user_hint(email: str) -> str:
    local_part = email.split("@", 1)[0].strip().lower()
    return re.split(r"[._+\-]", local_part, maxsplit=1)[0] or local_part


def _download_via_url(page: Page, download_url: str, download_dir: Path) -> Path:
    filename = (
        Path(unquote(urlsplit(download_url).path)).name or "dc_shipping_report.csv"
    )
    target_path = download_dir / filename
    response = page.context.request.get(download_url, timeout=60000)
    if not response.ok:
        raise RuntimeError(
            f"Helm export download failed with HTTP {response.status}: {download_url}"
        )
    target_path.write_bytes(response.body())
    return target_path


def _save_download(download: Any, download_dir: Path) -> Path:
    target_path = download_dir / download.suggested_filename
    download.save_as(target_path)
    return target_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(config: Config) -> int:
    _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
    _log_info(config.debug, f"Helm URL: {config.helm_url}")
    _log_info(config.debug, f"Download directory: {config.download_dir}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            login = LoginFlow(page, config)
            login.open()
            login.fill_credentials()
            login.submit()
            page.wait_for_load_state("domcontentloaded")
            login.verify()
            _log("Stage 2 / Step 2.1: Logged into Helm successfully")

            downloaded_path = download_shipping_report(page, config)
            _log(f"Stage 2 complete: DC Shipping Report saved to {downloaded_path}")
            return 0
        finally:
            try:
                context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    sys.exit(run(Config.load()))

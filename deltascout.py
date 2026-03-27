#!/usr/bin/env python3
from __future__ import annotations

import difflib
import hashlib
import json
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import yaml


ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env"
URLS_FILE = ROOT_DIR / "urls.yaml"
STATE_DIR = ROOT_DIR / ".deltascout"
SNAPSHOTS_DIR = STATE_DIR / "snapshots"
RUNS_DIR = STATE_DIR / "runs"
STATE_FILE = STATE_DIR / "state.json"

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = "DeltaScout/1.0"


class DeltaScoutError(Exception):
    """Raised for expected runtime/configuration errors."""


@dataclass(frozen=True)
class UrlEntry:
    name: str
    url: str
    render_js: bool = False
    wait_selector: str | None = None
    wait_timeout_seconds: int = 20


@dataclass(frozen=True)
class ChangeRecord:
    name: str
    url: str
    previous_snapshot: str
    current_snapshot: str
    diff: str


class VisibleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth > 0:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and data:
            self._parts.append(data)

    def get_text(self) -> str:
        combined = "\n".join(self._parts)
        combined = combined.replace("\r\n", "\n").replace("\r", "\n")
        combined = re.sub(r"[ \t\f\v]+", " ", combined)
        cleaned_lines = [line.strip() for line in combined.split("\n")]
        cleaned = "\n".join(cleaned_lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if not cleaned:
            return ""
        return cleaned + "\n"


def main() -> int:
    run_started_at = datetime.now(timezone.utc)
    run_id = run_started_at.strftime("%Y%m%dT%H%M%S%fZ")

    try:
        ensure_state_dirs()
        env = load_env(ENV_FILE)
        urls = load_urls(URLS_FILE)
        state = load_state(STATE_FILE)

        timeout_seconds = parse_timeout(env.get("REQUEST_TIMEOUT_SECONDS"))
        user_agent = env.get("USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT

        url_state: dict[str, dict[str, Any]] = state.setdefault("urls", {})
        changed_records: list[ChangeRecord] = []
        fetch_errors: list[dict[str, str]] = []

        checked = 0
        changed = 0
        unchanged = 0
        failed = 0
        initial_baselines = 0
        email_sent = False

        for entry in urls:
            checked += 1
            try:
                if entry.render_js:
                    html = fetch_html_rendered(entry, user_agent)
                else:
                    html = fetch_html(entry.url, timeout_seconds, user_agent)
                normalized_text = normalize_html(html)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                fetch_errors.append(
                    {"name": entry.name, "url": entry.url, "error": short_error(exc)}
                )
                continue

            snapshot_relative = save_snapshot(entry, run_id, normalized_text)
            existing = url_state.get(entry.url, {})
            previous_snapshot = existing.get("baseline_snapshot")

            if not previous_snapshot:
                initial_baselines += 1
                unchanged += 1
                url_state[entry.url] = {
                    "name": entry.name,
                    "baseline_snapshot": snapshot_relative,
                    "last_checked_at": run_started_at.isoformat(),
                }
                continue

            previous_text = read_snapshot_text(previous_snapshot)
            if previous_text is None:
                unchanged += 1
                url_state[entry.url] = {
                    "name": entry.name,
                    "baseline_snapshot": snapshot_relative,
                    "last_checked_at": run_started_at.isoformat(),
                }
                fetch_errors.append(
                    {
                        "name": entry.name,
                        "url": entry.url,
                        "error": f"missing baseline snapshot '{previous_snapshot}', reset baseline",
                    }
                )
                continue

            if previous_text == normalized_text:
                unchanged += 1
                url_state[entry.url] = {
                    "name": entry.name,
                    "baseline_snapshot": snapshot_relative,
                    "last_checked_at": run_started_at.isoformat(),
                }
                continue

            changed += 1
            changed_records.append(
                ChangeRecord(
                    name=entry.name,
                    url=entry.url,
                    previous_snapshot=previous_snapshot,
                    current_snapshot=snapshot_relative,
                    diff=build_unified_diff(
                        entry=entry,
                        previous_text=previous_text,
                        current_text=normalized_text,
                        previous_snapshot=previous_snapshot,
                        current_snapshot=snapshot_relative,
                    ),
                )
            )
            # Baseline update for changed URLs happens only after successful email send.
            url_state.setdefault(entry.url, {})
            url_state[entry.url]["name"] = entry.name

        should_send_email = bool(changed_records or fetch_errors)
        email_error: str | None = None
        if should_send_email:
            try:
                smtp_user = require_env(env, "GMAIL_SMTP_USER")
                smtp_pass = require_env(env, "GMAIL_APP_PASSWORD")
                recipients = parse_recipients(require_env(env, "ALERT_EMAIL_TO"))
                subject = build_email_subject(changed, failed, run_started_at)
                body = build_email_body(
                    run_started_at=run_started_at,
                    checked=checked,
                    changed=changed,
                    unchanged=unchanged,
                    failed=failed,
                    initial_baselines=initial_baselines,
                    changed_records=changed_records,
                    fetch_errors=fetch_errors,
                )
                send_email(smtp_user, smtp_pass, recipients, subject, body)
                email_sent = True
            except Exception as exc:  # noqa: BLE001
                email_error = short_error(exc)
                print(
                    f"Failed to send alert email: {email_error}",
                    file=sys.stderr,
                )

        if email_sent:
            for record in changed_records:
                url_state[record.url] = {
                    "name": record.name,
                    "baseline_snapshot": record.current_snapshot,
                    "last_checked_at": run_started_at.isoformat(),
                }

        save_state(STATE_FILE, state)

        run_log = {
            "run_id": run_id,
            "timestamp": run_started_at.isoformat(),
            "summary": {
                "checked": checked,
                "changed": changed,
                "unchanged": unchanged,
                "failed": failed,
                "initial_baselines": initial_baselines,
                "email_sent": email_sent,
            },
            "changed_urls": [
                {
                    "name": item.name,
                    "url": item.url,
                    "previous_snapshot": item.previous_snapshot,
                    "current_snapshot": item.current_snapshot,
                }
                for item in changed_records
            ],
            "errors": fetch_errors,
        }
        if email_error:
            run_log["email_error"] = email_error
        save_run_log(run_id, run_log)

        print_summary(
            checked=checked,
            changed=changed,
            unchanged=unchanged,
            failed=failed,
            email_sent=email_sent,
        )

        if should_send_email and not email_sent:
            return 2
        return 0

    except DeltaScoutError as exc:
        print(f"DeltaScout error: {exc}", file=sys.stderr)
        return 1


def ensure_state_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def require_env(env: dict[str, str], key: str) -> str:
    value = env.get(key) or ""
    if not value.strip():
        raise DeltaScoutError(
            f"required env var '{key}' not set in {ENV_FILE.name}"
        )
    return value.strip()


def parse_timeout(raw_value: str | None) -> int:
    if raw_value is None or not raw_value.strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = int(raw_value)
    except ValueError as exc:
        raise DeltaScoutError("REQUEST_TIMEOUT_SECONDS must be an integer") from exc
    if timeout <= 0:
        raise DeltaScoutError("REQUEST_TIMEOUT_SECONDS must be > 0")
    return timeout


def load_urls(path: Path) -> list[UrlEntry]:
    if not path.exists():
        raise DeltaScoutError(
            f"missing URL config file: {path.name} (create it from urls.yaml template)"
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise DeltaScoutError(f"{path.name} is empty")

    items: list[Any]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and isinstance(raw.get("urls"), list):
        items = raw["urls"]
    else:
        raise DeltaScoutError(
            f"{path.name} must be a list of entries or use top-level 'urls:' list"
        )

    entries: list[UrlEntry] = []
    seen_urls: set[str] = set()
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise DeltaScoutError(f"{path.name} entry #{idx} must be a mapping")
        url = str(item.get("url", "")).strip()
        name = str(item.get("name", "")).strip() or url
        render_js = parse_bool_field(item.get("render_js", False), "render_js", idx, path.name)

        wait_selector_raw = item.get("wait_selector")
        if wait_selector_raw is None:
            wait_selector: str | None = None
        elif isinstance(wait_selector_raw, str):
            wait_selector = wait_selector_raw.strip() or None
        else:
            raise DeltaScoutError(
                f"{path.name} entry #{idx} has invalid 'wait_selector' (must be string)"
            )

        wait_timeout_seconds = parse_int_field(
            item.get("wait_timeout_seconds", 20),
            "wait_timeout_seconds",
            idx,
            path.name,
            min_value=1,
        )

        if not url:
            raise DeltaScoutError(f"{path.name} entry #{idx} is missing 'url'")
        validate_url(url, idx, path.name)
        if url in seen_urls:
            raise DeltaScoutError(f"{path.name} contains duplicate URL: {url}")
        if not render_js and wait_selector is not None:
            raise DeltaScoutError(
                f"{path.name} entry #{idx} sets wait_selector but render_js is false"
            )
        seen_urls.add(url)
        entries.append(
            UrlEntry(
                name=name,
                url=url,
                render_js=render_js,
                wait_selector=wait_selector,
                wait_timeout_seconds=wait_timeout_seconds,
            )
        )

    if not entries:
        raise DeltaScoutError(f"{path.name} has no URLs to monitor")
    return entries


def validate_url(url: str, idx: int, filename: str) -> None:
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DeltaScoutError(
            f"{filename} entry #{idx} has invalid URL '{url}' (must be http/https)"
        )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "urls": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeltaScoutError(f"failed to parse {path.name}: {exc}") from exc
    if not isinstance(state, dict):
        raise DeltaScoutError(f"{path.name} must contain a JSON object")
    if "urls" not in state or not isinstance(state["urls"], dict):
        state["urls"] = {}
    if "version" not in state:
        state["version"] = 1
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def save_run_log(run_id: str, payload: dict[str, Any]) -> None:
    path = RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_html(url: str, timeout_seconds: int, user_agent: str) -> str:
    request = urllib_request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            encoding = response.headers.get_content_charset() or "utf-8"
            raw = response.read()
        return raw.decode(encoding, errors="replace")
    except urllib_error.HTTPError as exc:
        raise DeltaScoutError(f"HTTP {exc.code} for {url}") from exc
    except urllib_error.URLError as exc:
        raise DeltaScoutError(f"network error for {url}: {exc.reason}") from exc


def fetch_html_rendered(entry: UrlEntry, user_agent: str) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DeltaScoutError(
            "render_js=true requires Playwright. Install dependencies with "
            "'python -m pip install -r requirements.txt' and browser binaries with "
            "'python -m playwright install chromium'."
        ) from exc

    timeout_ms = entry.wait_timeout_seconds * 1000
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(user_agent=user_agent)
            page = context.new_page()
            try:
                page.goto(entry.url, wait_until="domcontentloaded", timeout=timeout_ms)
                if entry.wait_selector:
                    page.wait_for_selector(entry.wait_selector, timeout=timeout_ms)
                else:
                    try:
                        page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    except PlaywrightTimeoutError:
                        # Some sites keep long-lived connections; continue with current DOM.
                        pass
                return page.content()
            finally:
                context.close()
                browser.close()
    except PlaywrightTimeoutError as exc:
        if entry.wait_selector:
            waiting_for = f"selector '{entry.wait_selector}'"
        else:
            waiting_for = "page load"
        raise DeltaScoutError(
            f"Playwright timeout waiting for {waiting_for} on {entry.url}"
        ) from exc
    except DeltaScoutError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeltaScoutError(f"Playwright error for {entry.url}: {short_error(exc)}") from exc


def normalize_html(html: str) -> str:
    parser = VisibleTextExtractor()
    parser.feed(html)
    parser.close()
    text = parser.get_text()
    if text:
        return text
    fallback = re.sub(r"\s+", " ", html).strip()
    return (fallback + "\n") if fallback else ""


def save_snapshot(entry: UrlEntry, run_id: str, content: str) -> str:
    slug = make_entry_slug(entry)
    destination_dir = SNAPSHOTS_DIR / slug
    destination_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = destination_dir / f"{run_id}.txt"
    snapshot_path.write_text(content, encoding="utf-8")
    return snapshot_path.relative_to(STATE_DIR).as_posix()


def read_snapshot_text(snapshot_relative: str) -> str | None:
    path = STATE_DIR / snapshot_relative
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def make_entry_slug(entry: UrlEntry) -> str:
    normalized_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", entry.name.lower()).strip("-")
    normalized_name = normalized_name[:48] or "url"
    digest = hashlib.sha1(entry.url.encode("utf-8")).hexdigest()[:12]
    return f"{normalized_name}-{digest}"


def build_unified_diff(
    entry: UrlEntry,
    previous_text: str,
    current_text: str,
    previous_snapshot: str,
    current_snapshot: str,
) -> str:
    previous_lines = previous_text.splitlines(keepends=True)
    current_lines = current_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        previous_lines,
        current_lines,
        fromfile=f"{entry.name} ({previous_snapshot})",
        tofile=f"{entry.name} ({current_snapshot})",
        n=3,
    )
    text = "".join(diff).strip()
    return text or "(no textual diff produced)"


def parse_recipients(raw: str) -> list[str]:
    recipients = [item.strip() for item in raw.split(",") if item.strip()]
    if not recipients:
        raise DeltaScoutError("ALERT_EMAIL_TO must include at least one recipient")
    return recipients


def parse_bool_field(
    value: Any, field_name: str, idx: int, filename: str
) -> bool:
    if isinstance(value, bool):
        return value
    raise DeltaScoutError(
        f"{filename} entry #{idx} has invalid '{field_name}' (must be true/false)"
    )


def parse_int_field(
    value: Any,
    field_name: str,
    idx: int,
    filename: str,
    *,
    min_value: int,
) -> int:
    if isinstance(value, bool):
        raise DeltaScoutError(
            f"{filename} entry #{idx} has invalid '{field_name}' (must be integer)"
        )
    if not isinstance(value, int):
        raise DeltaScoutError(
            f"{filename} entry #{idx} has invalid '{field_name}' (must be integer)"
        )
    if value < min_value:
        raise DeltaScoutError(
            f"{filename} entry #{idx} has invalid '{field_name}' (must be >= {min_value})"
        )
    return value


def build_email_subject(changed: int, failed: int, started_at: datetime) -> str:
    timestamp = started_at.strftime("%Y-%m-%d %H:%M:%SZ")
    return f"[DeltaScout] {changed} changed, {failed} failed ({timestamp})"


def build_email_body(
    *,
    run_started_at: datetime,
    checked: int,
    changed: int,
    unchanged: int,
    failed: int,
    initial_baselines: int,
    changed_records: list[ChangeRecord],
    fetch_errors: list[dict[str, str]],
) -> str:
    lines: list[str] = []
    lines.append("DeltaScout run summary")
    lines.append(f"Timestamp (UTC): {run_started_at.isoformat()}")
    lines.append(f"Checked: {checked}")
    lines.append(f"Changed: {changed}")
    lines.append(f"Unchanged: {unchanged}")
    lines.append(f"Failed: {failed}")
    lines.append(f"Initial baselines: {initial_baselines}")
    lines.append("")

    if changed_records:
        lines.append("Changed URLs")
        lines.append("=" * 60)
        for idx, change in enumerate(changed_records, start=1):
            lines.append(f"{idx}. {change.name}")
            lines.append(f"URL: {change.url}")
            lines.append(f"From: {change.previous_snapshot}")
            lines.append(f"To:   {change.current_snapshot}")
            lines.append("")
            lines.append(change.diff)
            lines.append("-" * 60)
        lines.append("")

    if fetch_errors:
        lines.append("Fetch Errors")
        lines.append("=" * 60)
        for idx, item in enumerate(fetch_errors, start=1):
            lines.append(f"{idx}. {item['name']} ({item['url']})")
            lines.append(f"   {item['error']}")
        lines.append("")

    lines.append("Generated by DeltaScout.")
    return "\n".join(lines).strip() + "\n"


def send_email(
    smtp_user: str,
    smtp_password: str,
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_user
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


def short_error(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


def print_summary(
    *,
    checked: int,
    changed: int,
    unchanged: int,
    failed: int,
    email_sent: bool,
) -> None:
    print(
        "Run summary: "
        f"checked={checked} changed={changed} unchanged={unchanged} failed={failed} "
        f"email_sent={'yes' if email_sent else 'no'}"
    )


if __name__ == "__main__":
    sys.exit(main())

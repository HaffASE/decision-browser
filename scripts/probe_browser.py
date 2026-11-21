"""Probe the installed decision_browser driver against an owned loopback page.

Default mode is isolated. ``--existing`` is opt-in and never prints tab URLs,
target ids, or the DevTools endpoint. Signed-in provenance stays UNKNOWN, so
that check is AC03 and is not reported as PASS. Attach status is separate.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as importlib_metadata
import json
import platform
import plistlib
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from decision_browser.browser import (
    SIGNED_IN_PROVENANCE,
    BrowserBlocked,
    BrowserDriver,
    BrowserMode,
)

_ROOT = Path(__file__).resolve().parents[1]
_OUT_DIR = _ROOT / ".omx" / "implementation" / "driver"
_MARKER = "db_probe_marker"
_TITLE = "decision-browser-counter"
_FORBIDDEN_KEYS = {
    "url",
    "urls",
    "target_id",
    "target_ids",
    "targetId",
    "websocket_url",
    "websocket",
    "endpoint",
    "cookie",
    "cookies",
    "devtools",
}
_COUNTER_PAGE_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>decision-browser-counter</title></head>
<body>
<p id="count">0</p>
<button id="inc" type="button">inc</button>
<button id="reset" type="button">reset</button>
<script>
const count = document.getElementById("count");
let n = 0;
document.getElementById("inc").addEventListener("click", () => {
  n += 1;
  count.textContent = String(n);
});
document.getElementById("reset").addEventListener("click", () => {
  n = 0;
  count.textContent = "0";
});
</script>
</body>
</html>
"""


class _QuietHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = _COUNTER_PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _OwnedCounter:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{port}/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _package_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return "UNKNOWN"


def _chrome_version() -> str:
    plist_path = Path("/Applications/Google Chrome.app/Contents/Info.plist")
    if not plist_path.is_file():
        return "UNKNOWN"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return "UNKNOWN"
    version = info.get("CFBundleShortVersionString") if isinstance(info, Mapping) else None
    return version if isinstance(version, str) and version else "UNKNOWN"


def _blank_report(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "signed_in_provenance": SIGNED_IN_PROVENANCE,
        "signed_in_acceptance": "AC03",
        "attach_status": "BLOCKED",
        "blocked_code": None,
        "dom_title": None,
        "count_after_click": None,
        "count_after_reset": None,
        "png_file": None,
        "inventory_digest": None,
        "inventory_count": None,
        "foreign_mismatch": None,
        "residual_owned": None,
        "marker": "skipped",
        "chrome_version": _chrome_version(),
        "dependencies": {
            "decision-browser": _package_version("decision-browser"),
            "playwright": _package_version("playwright"),
            "httpx": _package_version("httpx"),
            "mcp": _package_version("mcp"),
            "pydantic": _package_version("pydantic"),
        },
        "platform": platform.platform(),
        "cleanup_errors": [],
    }


def _schema_errors(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []

    def walk(value: object, trail: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    errors.append(f"{trail}: non-string key")
                    continue
                if key in _FORBIDDEN_KEYS or key.lower() in _FORBIDDEN_KEYS:
                    errors.append(f"{trail}.{key}: forbidden")
                walk(child, f"{trail}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{trail}[{index}]")

    walk(report, "$")
    required = {
        "mode": ("isolated", "existing_chrome"),
        "signed_in_provenance": (SIGNED_IN_PROVENANCE,),
        "signed_in_acceptance": ("AC03",),
        "attach_status": ("PASS", "BLOCKED"),
        "marker": ("present", "absent", "skipped"),
    }
    for key, allowed in required.items():
        if report.get(key) not in allowed:
            errors.append(f"{key}: expected one of {allowed}")
    if report.get("signed_in_acceptance") == "PASS" or report.get("signed_in_provenance") == "PASS":
        errors.append("signed-in must stay AC03/UNKNOWN")
    for key in ("inventory_digest", "blocked_code", "dom_title", "png_file"):
        value = report.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{key}: expected string or null")
    for key in ("inventory_count", "count_after_click", "count_after_reset"):
        value = report.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            errors.append(f"{key}: expected int or null")
    for key in ("foreign_mismatch", "residual_owned"):
        value = report.get(key)
        if value is not None and not isinstance(value, bool):
            errors.append(f"{key}: expected bool or null")
    cleanup = report.get("cleanup_errors")
    if not isinstance(cleanup, list) or any(not isinstance(item, str) for item in cleanup):
        errors.append("cleanup_errors: expected list of strings")
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, Mapping) or any(not isinstance(item, str) for item in dependencies.values()):
        errors.append("dependencies: expected string map")
    if not isinstance(report.get("chrome_version"), str) or not isinstance(report.get("platform"), str):
        errors.append("version fields: expected strings")
    digest = report.get("inventory_digest")
    if isinstance(digest, str) and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        errors.append("inventory_digest: expected sha256 hex")
    return errors


def _inventory_fields(driver: BrowserDriver) -> tuple[str | None, int | None]:
    snapshot = driver.foreign_before
    if snapshot is None:
        return None, None
    return snapshot.digest, len(snapshot.targets)


async def _read_own_marker(page: Any) -> str:
    value = await page.evaluate(
        """(name) => {
            const parts = document.cookie ? document.cookie.split(";") : [];
            for (const part of parts) {
                const trimmed = part.trim();
                const eq = trimmed.indexOf("=");
                if (eq === -1) continue;
                if (trimmed.slice(0, eq) === name) return trimmed.slice(eq + 1);
            }
            return "";
        }""",
        _MARKER,
    )
    return value if isinstance(value, str) else ""


async def _exercise(driver: BrowserDriver, url: str, report: dict[str, Any], png_path: Path) -> None:
    await driver.page.goto(url)
    title = await driver.page.title()
    report["dom_title"] = title if isinstance(title, str) else None
    await driver.page.click("#inc")
    clicked = await driver.page.text_content("#count")
    await driver.page.click("#reset")
    reset = await driver.page.text_content("#count")
    report["count_after_click"] = int(clicked) if clicked is not None and clicked.isdigit() else None
    report["count_after_reset"] = int(reset) if reset is not None and reset.isdigit() else None
    if driver.mode is BrowserMode.isolated:
        await driver.page.evaluate(
            "(name) => { document.cookie = name + '=1; path=/'; }",
            _MARKER,
        )
        report["marker"] = "present" if await _read_own_marker(driver.page) == "1" else "absent"
    png = await driver.screenshot()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BrowserBlocked("screenshot_failed")
    png_path.write_bytes(png)
    report["png_file"] = png_path.name
    if report["dom_title"] != _TITLE or report["count_after_click"] != 1 or report["count_after_reset"] != 0:
        raise BrowserBlocked("owned_page_failed")


def _record_block(report: dict[str, Any], code: str) -> None:
    report["attach_status"] = "BLOCKED"
    report["blocked_code"] = report["blocked_code"] or code


async def run(mode: BrowserMode) -> int:
    report = _blank_report(mode.value)
    slug = "existing" if mode is BrowserMode.existing_chrome else "isolated"
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = _OUT_DIR / f"probe-{slug}.json"
    png_path = _OUT_DIR / f"probe-{slug}.png"
    fixture = _OwnedCounter()
    fixture.start()
    driver = BrowserDriver(mode)
    exercised = False
    try:
        try:
            await asyncio.wait_for(driver.start(), timeout=20)
            await asyncio.wait_for(_exercise(driver, fixture.url, report, png_path), timeout=20)
            exercised = True
        except BrowserBlocked as exc:
            _record_block(report, exc.code)
        except TimeoutError:
            _record_block(report, "probe_timeout")
        except Exception as exc:  # noqa: BLE001 - probe boundary records the exception class, not its text
            _record_block(report, type(exc).__name__)
        report["signed_in_provenance"] = SIGNED_IN_PROVENANCE
        report["signed_in_acceptance"] = "AC03"
        digest, count = _inventory_fields(driver)
        report["inventory_digest"] = digest
        report["inventory_count"] = count
        report["foreign_mismatch"] = driver.foreign_mismatch
        report["residual_owned"] = driver.residual_owned
        report["cleanup_errors"] = list(driver.cleanup_errors)
    finally:
        try:
            await asyncio.wait_for(driver.close(), timeout=5)
        except TimeoutError:
            _record_block(report, "probe_timeout")
        except Exception as exc:  # noqa: BLE001 - probe boundary records the exception class, not its text
            _record_block(report, type(exc).__name__)
        fixture.stop()
        report["cleanup_errors"] = list(driver.cleanup_errors)
        report["foreign_mismatch"] = driver.foreign_mismatch
        report["residual_owned"] = driver.residual_owned
        report["signed_in_provenance"] = SIGNED_IN_PROVENANCE
        report["signed_in_acceptance"] = "AC03"
        cleanup_ok = (
            report["cleanup_errors"] == []
            and report["foreign_mismatch"] is False
            and report["residual_owned"] is False
        )
        if exercised and cleanup_ok and report["blocked_code"] is None:
            report["attach_status"] = "PASS"
        else:
            report["attach_status"] = "BLOCKED"
            if report["blocked_code"] is None:
                report["blocked_code"] = "cleanup_failed"
    errors = _schema_errors(report)
    if errors:
        print(json.dumps({"schema": "FAIL", "error_count": len(errors)}))
        return 2
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "attach_status": report["attach_status"],
                "signed_in_provenance": report["signed_in_provenance"],
                "signed_in_acceptance": report["signed_in_acceptance"],
                "blocked_code": report["blocked_code"],
                "inventory_digest": report["inventory_digest"],
                "inventory_count": report["inventory_count"],
                "report": json_path.name,
            },
            sort_keys=True,
        )
    )
    return 0 if report["attach_status"] == "PASS" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe an owned loopback page. Default is isolated.")
    parser.add_argument(
        "--existing",
        action="store_true",
        help="Opt in to attaching to the selected already-running Chrome.",
    )
    args = parser.parse_args()
    mode = BrowserMode.existing_chrome if args.existing else BrowserMode.isolated
    raise SystemExit(asyncio.run(run(mode)))


if __name__ == "__main__":
    main()

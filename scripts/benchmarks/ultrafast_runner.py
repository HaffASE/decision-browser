"""Benchmark upstream policy/browser with an isolated profile and OpenAI transport."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
from common import RequestLedger, install_http_meter
from jev_runner import (
    checked_input,
    stop_chrome,
    stop_owned_daemon,
)


@contextmanager
def owned_chrome(executable, cleanup):
    """Only this disposable Chrome and its uniquely named Harness daemon are owned."""
    daemon_name = "codex-bench-ultra-" + uuid.uuid4().hex[:12]
    chrome = None
    with tempfile.TemporaryDirectory(
        prefix="codex-bench-ultra-"
    ) as directory:
        root = Path(directory)
        for key in (
            "BU_CDP_WS",
            "BU_BROWSER_ID",
            "BH_RUNTIME_DIR_SHARED",
            "BH_TMP_DIR_SHARED",
        ):
            os.environ.pop(key, None)
        os.environ.update(
            {
                "BU_NAME": daemon_name,
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "BH_RUNTIME_DIR": str(root / "r"),
                "BH_TMP_DIR": str(root / "t"),
                "NO_PROXY": ",".join(
                    filter(
                        None,
                        [
                            os.environ.get("NO_PROXY", ""),
                            "127.0.0.1",
                            "localhost",
                            "::1",
                        ],
                    )
                ),
            }
        )
        try:
            profile = root / "chrome"
            chrome = subprocess.Popen(
                [
                    executable,
                    "--headless=new",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={profile}",
                    "about:blank",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            port_file = profile / "DevToolsActivePort"
            deadline = time.monotonic() + 25
            while (
                not port_file.exists()
                and chrome.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if not port_file.exists():
                raise RuntimeError("isolated_chrome_unavailable")
            port = int(port_file.read_text().splitlines()[0])
            os.environ["BU_CDP_URL"] = f"http://127.0.0.1:{port}"
            yield
        finally:
            if "browser_harness.admin" in sys.modules:
                try:
                    cleanup["daemon"] = stop_owned_daemon(daemon_name)
                except Exception as exc:  # noqa: BLE001 -- Sanitize native process and cleanup errors.
                    cleanup["daemon"] = type(exc).__name__
            if chrome is not None:
                cleanup["chrome"] = stop_chrome(chrome)
    cleanup["temporary_profile"] = "removed"


def configure_viewport(width=1280, height=720):
    from jev_ultrafast.browser import Browser

    original = Browser.call

    def call(self, method, **params):
        if method == "Emulation.setDeviceMetricsOverride":
            params.update(width=width, height=height)
        return original(self, method, **params)

    Browser.call = call


def verify(agent, checks):
    results = []
    final_url = agent.browser.evaluate("location.href")
    for check in checks:
        if check["type"] == "url_equals":
            results.append(final_url == check["value"])
        elif check["type"] in {"text_equals", "value_equals"}:
            selector = json.dumps(check["target"])
            property_js = "el.value" if check["type"] == "value_equals" else "el.innerText.trim()"
            value = agent.browser.evaluate(
                "(() => {const el=document.querySelector(" + selector + ");"
                "return el ? " + property_js + " : null;})()"
            )
            results.append(value == check["value"])
        else:
            raise ValueError("unsupported_check")
    viewport = agent.browser.evaluate("({width:innerWidth,height:innerHeight})")
    return results, final_url, viewport


def run_case(data, provider=None):
    key = provider.decision_key if provider else os.environ.pop("OPENAI_API_KEY", "")
    if not key or key.startswith("sk-or-"):
        raise ValueError("missing_openai_key")
    for name in ("TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY", "OPENROUTER_API_KEY"):
        os.environ.pop(name, None)
    # Upstream reads these before calling the replaced transport. No external
    # TypeSafe/helper credential is loaded, and no request is sent to them.
    os.environ["TYPESAFE_API_KEY"] = "benchmark_transport"
    os.environ["TEXT_MODEL_API_KEY"] = "benchmark_transport"
    model_name = provider.decision_model if provider else "gpt-6-luna"
    os.environ["TYPESAFE_MODEL"] = model_name
    os.environ["TEXT_MODEL"] = "gpt-6-luna"
    os.environ["TEXT_MODEL_BASE_URL"] = "https://api.openai.com/v1"
    ledger = provider or RequestLedger(
        data.get("max_calls", 12), data.get("max_input_bytes", 131072),
        float(data.get("max_cost_usd", .25))
    )
    if provider is None:
        install_http_meter(ledger)
    cleanup = {}
    answer = {
        "status": "error",
        "model": model_name,
        "checks": [],
        "reason": "runner_error",
    }
    started = time.monotonic()
    agent = None
    client = httpx.Client(
        http2=True, timeout=25, follow_redirects=False, trust_env=False
    )
    try:
        with owned_chrome(data["browser_executable"], cleanup):
            sys.path.insert(0, str(data["source_root"]))
            import jev_ultrafast.agent as loop
            from jev_ultrafast import model
            from openai_transport import TransportError, make_post_json

            configure_viewport()
            loop.MAX_STEPS = data.get("max_steps", 8)
            model.post_json = provider.ultrafast_transport(client) if provider else make_post_json(key, client)
            handler = signal.getsignal(signal.SIGALRM)

            def expired(_signum, _frame):
                raise TimeoutError("task_deadline_exceeded")

            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, data.get("timeout_s", 75))
            task_started = time.monotonic()
            try:
                agent = loop.Agent(data["start_url"], data["goal"], screenshots=False)
                for _state in agent.run():
                    pass
                answer.update(
                    status=agent.state["status"],
                    reason="goal_achieved"
                    if agent.state["status"] == "done"
                    else "agent_blocked",
                )
            except Exception as exc:  # noqa: BLE001 -- Sanitize native process and cleanup errors.
                code = str(exc) if isinstance(exc, TransportError) else type(exc).__name__
                if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
                    code = type(exc).__name__
                answer.update(
                    status="error",
                    reason=code,
                    error_type=type(exc).__name__,
                )
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, handler)
                task_finished = time.monotonic()
                if agent is not None:
                    answer.update(
                        agent_decisions=len(agent.state["decisions"]),
                        text_calls=len(agent.state["text_calls"]),
                        steps=len(agent.state["history"]),
                        actions=[h["kind"] for h in agent.state["history"]],
                        agent_elapsed_ms=agent.state["elapsed_ms"],
                    )
                    try:
                        checks, url, viewport = verify(agent, data.get("checks", []))
                        answer.update(
                            checks=checks, final_url=url, benchmark_viewport=viewport
                        )
                    except Exception as exc:  # noqa: BLE001 -- Sanitize native process and cleanup errors.
                        answer["verification_error"] = type(exc).__name__
                    finally:
                        try:
                            agent.close()
                            cleanup["agent"] = "closed"
                        except Exception as exc:  # noqa: BLE001 -- Sanitize native process and cleanup errors.
                            cleanup["agent"] = type(exc).__name__
                answer.update(
                    setup_ms=round((task_started - started) * 1000),
                    task_ms=round((task_finished - task_started) * 1000),
                    verification_ms=round((time.monotonic() - task_finished) * 1000),
                )
                model.CLIENT.close()
    except Exception as exc:  # noqa: BLE001 -- Sanitize native process and cleanup errors.
        answer.update(
            status="error", reason=type(exc).__name__, error_type=type(exc).__name__
        )
    finally:
        client.close()
        answer.update(
            usage=ledger.snapshot(),
            cleanup=cleanup,
            wall_total_ms=round((time.monotonic() - started) * 1000),
        )
    return answer


if __name__ == "__main__":
    try:
        answer = run_case(checked_input())
    except Exception as exc:  # noqa: BLE001 -- Sanitize native process and cleanup errors.
        answer = {"status": "runner_error", "error_type": type(exc).__name__}
    print(json.dumps(answer, ensure_ascii=False), flush=True)

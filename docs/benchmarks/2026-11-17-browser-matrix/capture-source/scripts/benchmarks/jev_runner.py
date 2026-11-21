"""Run one jev-ra benchmark case in a disposable Chrome, without Keychain access.

Input: JSON on stdin; the caller supplies OPENAI_API_KEY in the environment.
Output: a single JSON object with sanitized status, timing and request usage.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from common import RequestLedger, install_http_meter

OPENAI_ORIGIN = "https://api.openai.com"
OPENAI_MODEL = "gpt-6-luna"
SAFE_REASON = re.compile(r"^[a-zA-Z0-9_:. -]{1,120}$")


def checked_input(data=None) -> dict[str, object]:
    if data is None:
        data = json.load(sys.stdin)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("start_url"), str)
        or not isinstance(data.get("goal"), str)
    ):
        raise TypeError("start_url and goal must be strings")
    if not re.match(r"^https?://", data["start_url"]):
        raise ValueError("start_url must use http or https")
    if not isinstance(data.get("values", {}), dict) or not isinstance(
        data.get("checks", []), list
    ):
        raise TypeError("values and checks have invalid types")
    executable = data.get("browser_executable")
    if (not isinstance(executable, str) or not Path(executable).is_absolute()
            or not Path(executable).is_file() or not os.access(executable, os.X_OK)):
        raise ValueError("browser_executable must be an absolute executable file")
    source = data.get("source_root")
    if not isinstance(source, str) or not Path(source).is_absolute() or not Path(source).is_dir():
        raise ValueError("source_root must be an absolute source directory")
    if any(not isinstance(k, str) or not isinstance(v, str)
           for k, v in data.get("values", {}).items()):
        raise ValueError("provided values must map strings to strings")
    for check in data.get("checks", []):
        if (
            not isinstance(check, dict)
            or check.get("type")
            not in {"text_equals", "value_equals", "url_equals"}
            or not isinstance(check.get("value"), str)
        ):
            raise ValueError("invalid check")
        if check["type"] in {"text_equals", "value_equals"} and (
            not isinstance(check.get("target"), str) or not check["target"].strip()
        ):
            raise ValueError("DOM checks require a CSS target")
    if (
        "max_calls" in data
        and "max_provider_calls" in data
        and data["max_calls"] != data["max_provider_calls"]
    ):
        raise ValueError("conflicting provider call budgets")
    if "max_provider_calls" in data:
        data["max_calls"] = data["max_provider_calls"]
    for name, default, maximum in (
        ("max_steps", 12, 200),
        ("max_calls", 30, 200),
        ("max_input_bytes", 131072, 10485760),
        ("timeout_s", 90, 600),
    ):
        value = data.get(name, default)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value <= 0
            or value > maximum
            or (name != "timeout_s" and type(value) is not int)
        ):
            raise ValueError(f"invalid {name}")
    cost = data.get("max_cost_usd", .25)
    if type(cost) not in (int, float) or not math.isfinite(cost) or not 0 < cost <= .25:
        raise ValueError("invalid max_cost_usd")
    return data


def stop_chrome(chrome: subprocess.Popen) -> str:
    if chrome.poll() is not None:
        return "already_exited"
    try:
        os.killpg(chrome.pid, signal.SIGTERM)
        chrome.wait(timeout=5)
        return "terminated"
    except subprocess.TimeoutExpired:
        os.killpg(chrome.pid, signal.SIGKILL)
        chrome.wait(timeout=5)
        return "killed_after_timeout"
    except (OSError, ProcessLookupError):
        return "cleanup_error"


def stop_owned_daemon(name: str) -> str:
    """Stop only our private harness daemon and reap its child PID promptly."""
    from browser_harness import _ipc
    from browser_harness.admin import restart_daemon

    pid = _ipc.identify(name, timeout=2)
    if pid is None:
        return "not_running_or_unidentified"
    try:
        exited, _ = os.waitpid(pid, os.WNOHANG)
        if exited == pid:
            return "already_reaped"
    except ChildProcessError:
        # Use the library's identity-checked fallback for anything not our child.
        restart_daemon(name)
        return "fallback_stop_requested"

    sock = None
    try:
        sock, token = _ipc.connect(name, timeout=3)
        response = _ipc.request(sock, token, {"meta": "shutdown"})
        if not isinstance(response, dict) or response.get("ok") is not True:
            restart_daemon(name)
            return "fallback_stop_requested"
    except (OSError, ValueError, RuntimeError, TimeoutError):
        restart_daemon(name)
        return "fallback_stop_requested"
    finally:
        if sock is not None:
            sock.close()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            exited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return "reaped"
        if exited == pid:
            return "reaped"
        time.sleep(0.05)
    # PID cannot be recycled while our unreaped child exists.
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            exited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return "terminated"
        if exited == pid:
            return "terminated"
        time.sleep(0.05)
    return "cleanup_timeout"


def verify(agent, checks):
    """Read final state independently; check specifications never enter policy."""
    page = agent.session.observe()
    results = []
    for check in checks:
        if check["type"] in {"text_equals", "value_equals"}:
            selector = json.dumps(check["target"], ensure_ascii=False)
            property_js = "el.value" if check["type"] == "value_equals" else "el.innerText.trim()"
            value = agent.session.evaluate(
                "(() => { const el = document.querySelector("
                + selector + "); return el ? " + property_js + " : null; })()"
            )
            results.append(value == check["value"])
        elif check["type"] == "url_equals":
            results.append(page.get("url", "") == check["value"])
        else:
            raise ValueError("unsupported_check")
    viewport = agent.session.evaluate("({width:innerWidth,height:innerHeight})")
    return results, page.get("url", ""), viewport


def run_case(data: dict[str, object], provider=None) -> dict[str, object]:
    key = provider.decision_key if provider else os.environ.pop("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("sk-or-"):
        raise ValueError("OPENAI_API_KEY is missing or is not an OpenAI key")
    for field in (
        "JEV_RA_API_KEY",
        "TYPESAFE_API_KEY",
        "OPENROUTER_API_KEY",
        "JEV_RA_TEXT_MODEL",
        "JEV_RA_TEXT_BASE_URL",
        "JEV_RA_TEXT_API_KEY",
        "BU_CDP_WS",
        "BU_BROWSER_ID",
    ):
        os.environ.pop(field, None)
    started = time.monotonic()
    ledger = provider or RequestLedger(
        int(data.get("max_calls", 12)), int(data.get("max_input_bytes", 131072)),
        float(data.get("max_cost_usd", .25))
    )
    if provider is None:
        install_http_meter(ledger)
    endpoint = provider.decision_endpoint if provider else OPENAI_ORIGIN + "/v1/decisions"
    model = provider.decision_model if provider else OPENAI_MODEL
    answer: dict[str, object] = {"model": model, "status": "error", "checks": []}
    daemon_name = "codex-bench-jev-" + uuid.uuid4().hex[:12]
    chrome = agent = None
    speculations = []
    cleanup: dict[str, object] = {}
    tmp = tempfile.TemporaryDirectory(prefix="codex-bench-jev-")
    try:
        root = Path(tmp.name)
        os.environ.update(
            {
                "BU_NAME": daemon_name,
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "BH_RUNTIME_DIR": str(root / "r"),
                "BH_TMP_DIR": str(root / "t"),
                "JEV_RA_ENDPOINT": endpoint,
                "JEV_RA_MODEL": model,
                "JEV_RA_VIEWPORT": str(data.get("viewport", "1280x720")),
                "JEV_RA_TIMEOUT_S": str(data.get("timeout_s", 90)),
                "JEV_RA_MAX_STEPS": str(int(data.get("max_steps", 12))),
                "JEV_RA_MAX_DECISIONS": str(int(data.get("max_calls", 30))),
                "JEV_RA_HUMAN_WAIT_S": "0",
                "JEV_RA_NOTIFY": "0",
            }
        )
        os.environ.pop("BH_RUNTIME_DIR_SHARED", None)
        os.environ.pop("BH_TMP_DIR_SHARED", None)
        previous_no_proxy = os.environ.get("NO_PROXY", "")
        os.environ["NO_PROXY"] = ",".join(
            filter(None, [previous_no_proxy, "127.0.0.1", "localhost", "::1"])
        )
        profile = root / "chrome"
        browser_executable = str(data["browser_executable"])
        version = subprocess.run(
            [browser_executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=10,
            check=False,
        )
        answer["browser_executable"] = str(Path(browser_executable).resolve())
        answer["browser_version"] = (
            version.stdout.strip() if version.returncode == 0 else "unknown"
        )
        chrome = subprocess.Popen(
            [
                browser_executable,
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
            env=os.environ.copy(),
            start_new_session=True,
        )
        port_file = profile / "DevToolsActivePort"
        startup_deadline = time.monotonic() + 25
        while (
            not port_file.exists()
            and chrome.poll() is None
            and time.monotonic() < startup_deadline
        ):
            time.sleep(0.2)
        if not port_file.exists():
            raise RuntimeError("isolated Chrome did not expose DevTools")
        port = int(port_file.read_text().splitlines()[0])
        os.environ["BU_CDP_URL"] = f"http://127.0.0.1:{port}"
        sys.path.insert(0, str(data["source_root"]))
        from jev_ra.agent import Agent, Speculation
        from jev_ra.config import load

        init = Speculation.__init__

        def observe_speculation(instance, *args, **kwargs):
            init(instance, *args, **kwargs)
            speculations.append(instance)

        Speculation.__init__ = observe_speculation
        is_openai = endpoint == OPENAI_ORIGIN + "/v1/decisions"
        cfg = load({**os.environ, "OPENAI_API_KEY" if is_openai else "JEV_RA_API_KEY": key})
        if cfg.endpoint != endpoint or cfg.model != model or cfg.provider != ("openai" if is_openai else "typesafe"):
            raise ValueError("provider routing configuration mismatch")
        agent = Agent(config=cfg)
        answer["configured_viewport"] = {"width": cfg.viewport.width, "height": cfg.viewport.height}
        setup_finished = time.monotonic()
        prior_handler = signal.getsignal(signal.SIGALRM)

        def timed_out(_signum, _frame):
            raise TimeoutError("benchmark task deadline exceeded")

        signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, float(data.get("timeout_s", 90)))
        try:
            result = agent.run(
                goal=data["goal"],
                values=data.get("values", {}),
                max_steps=int(data.get("max_steps", 12)),
                url=data["start_url"],
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior_handler)
        task_finished = time.monotonic()
        answer.update(
            {
                "status": result.status,
                "reason": result.reason
                if SAFE_REASON.fullmatch(result.reason or "")
                else "redacted",
                "agent_decisions": result.decisions,
                "agent_speculations": result.speculations,
                "steps": len(result.steps),
                "actions": [
                    str(step.get("operation", "unknown")) for step in result.steps
                ],
                "setup_ms": round((setup_finished - started) * 1000),
                "task_ms": round((task_finished - setup_finished) * 1000),
                "agent_elapsed_ms": result.elapsed_ms,
            }
        )
    except Exception as exc:  # noqa: BLE001 - sanitized benchmark process boundary
        answer["error_type"] = type(exc).__name__
    finally:
        verification_started = time.monotonic()
        if agent is not None:
            try:
                checks, final_url, viewport = verify(agent, data.get("checks", []))
                answer.update(checks=checks, final_url=final_url, benchmark_viewport=viewport)
            except Exception as exc:  # noqa: BLE001 -- Preserve final-state evidence on native failures.
                answer["verification_error"] = type(exc).__name__
        answer["verification_ms"] = round((time.monotonic() - verification_started) * 1000)
        answer["through_verification_ms"] = round((time.monotonic() - started) * 1000)
        settle_started = time.monotonic()
        settle_deadline = settle_started + 27
        for guess in speculations:
            guess.thread.join(timeout=max(0, settle_deadline - time.monotonic()))
        answer["settle_ms"] = round((time.monotonic() - settle_started) * 1000)
        answer["unfinished_speculations"] = sum(
            guess.thread.is_alive() for guess in speculations
        )
        if agent is not None:
            try:
                agent.close()
                cleanup["agent"] = "closed"
            except Exception as exc:  # noqa: BLE001 - record cleanup without data
                cleanup["agent"] = type(exc).__name__
        if "browser_harness.admin" in sys.modules:
            try:
                cleanup["daemon"] = stop_owned_daemon(daemon_name)
            except Exception as exc:  # noqa: BLE001 - record cleanup without data
                cleanup["daemon"] = type(exc).__name__
        if chrome is not None:
            cleanup["chrome"] = stop_chrome(chrome)
        try:
            tmp.cleanup()
            cleanup["temporary_profile"] = "removed"
        except Exception as exc:  # noqa: BLE001 - record cleanup without data
            cleanup["temporary_profile"] = type(exc).__name__
        answer["cleanup"] = cleanup
        answer["usage"] = ledger.snapshot()
        answer["wall_total_ms"] = round((time.monotonic() - started) * 1000)
    return answer


def main() -> int:
    try:
        result = run_case(checked_input())
    except Exception as exc:  # noqa: BLE001 - sanitized command boundary
        result = {"status": "runner_error", "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("status") not in {"runner_error", "error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the published standalone Terra agent with Playwright's document-scoped refs."""
import importlib.util
import json
import re
from pathlib import Path

BASE_AGENT = (Path(__file__).resolve().parents[2] / "docs/benchmarks"
              / "2026-11-03-terra-medium" / "agent.py")


def load_agent():
    spec = importlib.util.spec_from_file_location("standalone_terra", BASE_AGENT)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    # The original guard still requires an exact match in the current snapshot.
    # After navigation Playwright may prefix an element number with its frame.
    agent.REF = re.compile(r"(?:f[0-9]+)?e[0-9]+")
    return agent


if __name__ == "__main__":
    try:
        result = load_agent().main()
    except Exception as exc:  # noqa: BLE001 -- publish only the exception type at the worker boundary.
        result = {"status": "runner_error", "reason": type(exc).__name__, "checks": []}
    print(json.dumps(result), flush=True)

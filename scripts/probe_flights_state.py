"""Replay scripted Flights clicks with no model API calls, then report the pilot's state check.

A local scripted provider replaces OpenAI and a temporary synthetic ledger replaces the
real one. The browser is a fresh isolated sandboxed Chromium with no credentials.
The output adds bounded public UI labels for local diagnosis; it is not a pilot report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from decision_browser.decision_codec import Decision
from decision_browser.experiment_budget import ExperimentLedger
from decision_browser.flights_continuation import (
    DATE_DONE,
    JFK,
    LAX,
    STATE_SCRIPT,
    state_shape,
)
from decision_browser.flights_pilot import FlightsRunManager, pilot_request
from decision_browser.runner import RunManager, RunnerError

_TRIP = (("click", "Round trip"), ("click", "One way"))
_AIRPORTS = (("click", "Where from?"), ("fill", "Where from?"), ("click", JFK),
             ("click", "Where to?"), ("fill", "Where to?"), ("click", LAX))
_DATE = (("click", "Departure"), ("click", "November 10, 2026"), ("click", DATE_DONE))
SCRIPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "trip-menu": _TRIP,
    "where-from": (*_TRIP, ("click", "Where from?")),
    "airports": _TRIP + _AIRPORTS,
    "date": _TRIP + _AIRPORTS + _DATE,
    "search": (*_TRIP, *_AIRPORTS, *_DATE, ("click", "Search"), ("DONE", "")),
}


class ScriptedProvider:
    def __init__(self, names: tuple[tuple[str, str], ...]) -> None:
        self.names, self.calls, self.offered, self.waits = names, 0, [], 0
        # The script advances only on acknowledged effects: a decision the host
        # discards as stale is asked again, as a model would be.
        self.progress: Any = lambda: self.calls

    async def decide(self, questions: Any, input_text: str, **_kwargs: Any) -> Decision:
        data = json.loads(input_text)
        self.offered.append({"state": data.get("state"), "candidates": sorted(
            f"{group}:{item['name']}" for group, bag in data["candidates"].items() for item in bag.values())})
        index = self.progress()
        if index >= len(self.names):
            raise RunnerError("probe_complete")
        operation, wanted = self.names[index]
        self.calls += 1
        if operation == "DONE":
            if (data.get("state") or {}).get("loading") and "wait" in data["candidates"] and self.waits < 15:
                self.waits += 1
                return Decision("wait", next(iter(data["candidates"]["wait"])), 1.0)
            return Decision("DONE", None, 1.0)
        group = "submit" if operation == "click" and wanted == "Search" and not any(
            item["name"] == wanted for item in data["candidates"].get("click", {}).values()) else operation
        matches = [key for key, item in data["candidates"].get(group, {}).items() if item["name"] == wanted]
        if not matches and "wait" in data["candidates"] and self.waits < 5:
            # As a model would: the target may render after the post-action observation.
            self.waits += 1
            return Decision("wait", next(iter(data["candidates"]["wait"])), 1.0)
        if len(matches) != 1:
            raise RunnerError("probe_target_absent")
        return Decision(group, matches[0], 1.0)

    async def aclose(self) -> None:
        pass


class Probe(FlightsRunManager):
    def __init__(self, ledger: ExperimentLedger, names: tuple[tuple[str, str], ...]) -> None:
        self.local = ScriptedProvider(names)
        super().__init__(self.local, ledger, continuation=True)  # type: ignore[arg-type]
        self.local.progress = lambda: sum(row.get("acknowledged") is True and row.get("operation") != "wait"
                                          for row in self.trace)
        self.capture: dict[str, Any] | None = None
        self.failure_capture: dict[str, Any] | None = None
        self.failure_exception: dict[str, Any] | None = None
        self.screenshot: Path | None = None
        self.inventory_capture: list[dict[str, Any]] | None = None

    def _questions(self, run: Any, obs: Any) -> Any:
        try:
            return super()._questions(run, obs)
        except RunnerError as exc:
            if exc.code == "pilot_out_of_scope":
                self.inventory_capture = [
                    {"name": str(node.get("name", ""))[:40], "role": node.get("role"),
                     "geometry": {key: candidate.geometry.get(key) for key in ("visible", "occluded", "top", "y", "height")}
                     if candidate.geometry else None}
                    for candidate in obs.candidates.values()
                    for node in [candidate.semantic_by_operation.get("click", {}).get("node") or {}]
                    if any(word in str(node.get("name", "")).lower() for word in ("way", "trip"))][:8]
                self.inventory_capture.append({"candidates": len(obs.candidates)})
            raise

    async def _verify(self, run: Any, finish: bool = False, due_to_quota: bool = False) -> bool:
        verified = await super()._verify(run, finish, due_to_quota)
        if finish and not verified and self.screenshot is not None and run.driver is not None:
            await run.driver.page.screenshot(path=str(self.screenshot))
        return verified

    def _apply(self, run: Any, exc: BaseException) -> None:
        # Local diagnosis only: exception class and closed code, never its message.
        self.failure_exception = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
        super()._apply(run, exc)

    async def _choose(self, run: Any, obs: Any) -> Any:
        waits = self.local.waits
        try:
            action = await super()._choose(run, obs)
            if self.local.waits > waits and run.dom is not None:
                self.capture = await self.inspect(run, obs)  # latest missing scripted target
            return action
        except RunnerError as exc:
            if exc.code != "stale" and run.dom is not None:
                self.failure_capture = await self.inspect(run, obs)
                if self.screenshot is not None:  # local file for diagnosis only
                    await run.driver.page.screenshot(path=str(self.screenshot))
            raise

    async def inspect(self, run: Any, obs: Any) -> dict[str, Any]:
        value = await run.dom._world.evaluate(STATE_SCRIPT)
        evidence = value.get("evidence") or {}
        form = evidence.get("form") or {}
        bindings = []
        for bag in RunManager._questions(self, run, obs)[2].values():
            for item in bag.values():
                node = item["semantic"].get("node") or {}
                if not node:
                    continue
                state = node.get("state") or {}
                geometry = (obs.candidates.get(item["token"]) or type("G", (), {"geometry": {}})).geometry or {}
                bindings.append({"operation": item["operation"],
                                 "canonical": self._canonical(item["operation"], node, item["token"], obs.id),
                                 "day_label": (self._day_labels[1].get(item["token"])
                                               if self._day_labels and self._day_labels[0] == obs.id else None),
                                 "name": str(node.get("fullName", node.get("name", "")))[:80],
                                 "role": node.get("role"), "tag": state.get("tag"),
                                 "expanded": (state.get("aria") or {}).get("expanded"),
                                 "visible": geometry.get("visible"), "occluded": geometry.get("occluded")})
        return {"state_shape": state_shape(value),
                "form_labels": {key: [{"label": item.get("label", "")[:80], "expanded": item.get("expanded")}
                                      for item in form.get(key, [])] for key in ("origin", "destination", "departure")},
                "form_controls": [name[:80] for name in form.get("controls", [])],
                "dialogs": value.get("dialogs"), "binding_count": len(bindings),
                "phase": self._state.get("phase"),
                "calendar_targets": [item for item in bindings if item["name"].startswith(("10", "Done"))][:6],
                "trip_like": [item for item in bindings if item["role"] in {"combobox", "button"}
                              and any(word in item["name"].lower() for word in ("way", "trip"))][:8],
                "bindings": bindings[:60]}


async def main(output: Path, names: tuple[tuple[str, str], ...], screenshot: Path | None = None) -> dict[str, Any]:
    with (tempfile.TemporaryDirectory(prefix="flights-probe-") as temp,
          ExperimentLedger(Path(temp) / "synthetic.jsonl", create=True) as ledger):
        host = Probe(ledger, names)
        host.screenshot = screenshot
        try:
            request = pilot_request(180)
            request.limits.max_steps = request.limits.max_provider_calls = len(names) + 20
            ledger.reserve_step()
            run_id = host.start_task(request)
            assert host._run is not None and host._run.task is not None
            await host._run.task
            result = host.get_task(run_id).model_dump(mode="json")
            report = {"real_model_api_calls": 0, "script": list(names),
                      "scripted_decisions": host.local.calls, "scripted_waits": host.local.waits,
                      "reached_decision_after_script": len(host.local.offered) > len(names),
                      "terminal": result["terminal"], "failure_code": host.failure_code,
                      "result_reason": result["reason"], "result_evidence": result["evidence"],
                      "scope_reason": host.scope_reason, "diagnostics": host.diagnostics,
                      "oracle": {key: host.report.get(key) for key in ("status", "reasons")},
                      "offered": host.local.offered,
                      "actions": [{key: row.get(key) for key in ("operation", "name", "outcome", "post_observation")}
                                  for row in host.trace],
                      "capture": host.capture, "failure_capture": host.failure_capture,
                      "failure_exception": host.failure_exception,
                      "inventory_capture": host.inventory_capture}
        finally:
            await host.aclose()
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--script", choices=sorted(SCRIPTS), default="where-from")
    parser.add_argument("--screenshot", type=Path, help="save a local PNG when the probe stops")
    args = parser.parse_args()
    report = asyncio.run(main(args.output, SCRIPTS[args.script], args.screenshot))
    print(json.dumps({key: report[key] for key in (
        "scripted_decisions", "scripted_waits", "reached_decision_after_script", "terminal", "failure_code", "scope_reason", "oracle")}))

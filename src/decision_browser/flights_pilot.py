"""One authorized public Flights experiment through the production Decisions runner.

Run with an already injected OPENAI_API_KEY. This module never loads credentials
from files or Keychain. --initialize is only for the first authorized attempt;
all retries must use the same ledger without that flag.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .action_context import action_record
from .budget import BudgetExceeded
from .contracts import Limits, PrivacyPolicy, TaskRequest
from .decision_codec import ChoiceQuestion
from .dom import Action, Observation
from .experiment_budget import ExperimentBudgetError, ExperimentLedger
from .flights_continuation import (
    COMMIT_QUERY,
    DATE_DONE,
    DAY_LABEL_SCRIPT,
    JFK,
    LAX,
    ORIGIN,
    PICKERS,
    STATE_REASONS,
    STATE_SCRIPT,
    PilotStateError,
    ProjectedProvider,
    allowed_url,
    canonical_control,
    day_label,
    project_input,
    project_report,
    state_shape,
    validate_state,
)
from .flights_evidence import (
    FLIGHTS_EVIDENCE_SCRIPT,
    evidence_digest,
    validate_flights_evidence,
)
from .flights_provider import FlightsProvider
from .flights_query import DATE_LABEL, query_is_current
from .runner import RunManager, RunnerError, _Run

_SAFE_FAILURES = frozenset({
    "experiment_calls", "experiment_steps", "experiment_input_bytes", "experiment_estimated_cost",
    "experiment_expired_or_finished", "experiment_deadline", "flights_decisions_only",
    "flights_missing_bound_value", "pilot_out_of_scope", "navigation", "outcome_unknown", "blocked",
    "ledger_corrupt", "ledger_missing_or_replaced", "ledger_unavailable_or_corrupt",
    "ledger_closed_or_uncertain", "ledger_write_uncertain", "clock_rollback",
    "ledger_state_changed", "ledger_size_limit",
    "invalid_flights_evidence", "launch_failed", "http_error", "transport_error",
})


COMMIT_SETTLE_SECONDS = 1.5
RESULTS_SETTLE_SECONDS = 8.0

# Closed, nonsecret reasons behind the shared pilot_out_of_scope failure code.
SCOPE_REASONS = STATE_REASONS | frozenset({
    "request_mismatch", "limits_exceed_pilot", "run_not_ready", "admission_scope", "proof_run_mismatch",
    "budget_exhausted", "unsafe_destination", "duplicate_offered_control", "hidden_trip_menu_mismatch",
    "control_count_trip", "control_count_origin", "control_count_destination", "control_count_departure",
    "forbidden_control_name",
})


def safe_failure(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code in _SAFE_FAILURES else "unclassified_failure"


def _same_text(left: Any, right: Any) -> bool | None:
    return left == right if type(left) is str and left and type(right) is str and right else None


def _url_matches(run: _Run, obs: Observation) -> bool | None:
    try:
        return _same_text(obs.url, run.driver.page.url)
    except Exception:  # noqa: BLE001 — optional metadata must not mask the primary outcome
        return None


def pilot_request(seconds: float) -> TaskRequest:
    return TaskRequest(
        goal=("Search Google Flights for one-way flights from John F. Kennedy "
              "International Airport JFK to Los Angeles International Airport LAX, "
              f"departing {DATE_LABEL}, 1 adult, economy including Basic. "
              "Select the exact airport suggestions, confirm the calendar date, "
              "then Search. Stop when the correct priced flight results are visible. "
              "Do not select a flight, book, sign in, or solve CAPTCHA."),
        start_url="https://www.google.com/travel/flights?hl=en", mode="isolated",
        values={"Where from?": "JFK", "Where to?": "LAX"}, checks=[],
        limits=Limits(max_steps=40, max_provider_calls=40, max_seconds=min(600., seconds),
                      max_input_bytes=512*1024), privacy=PrivacyPolicy(), visual=False,
    )


class FlightsRunManager(RunManager):
    """Only adds cumulative limits and an independent read-only success oracle."""
    def __init__(self, provider: FlightsProvider, ledger: ExperimentLedger, *, continuation: bool = False) -> None:
        self.continuation = continuation
        self._state: dict[str, Any] = {}
        wrapped = ProjectedProvider(provider, lambda raw: project_input(raw, self._state)) if continuation else provider
        super().__init__(lambda: wrapped, admission_mode="task_scope")
        self.ledger = ledger
        self.search_action: dict[str, Any] | None = None
        self.date_selection: dict[str, Any] | None = None
        self.pre_search: dict[str, Any] | None = None
        self.report: dict[str, Any] = {"status": "NOT_VERIFIED", "reasons": ["not_run"]}
        self.trace: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.failure_code: str | None = None
        self.continuations = 0
        self._spent: set[str] = set()
        self._completed: str | None = None
        self._committed: frozenset[str] = frozenset()
        self._generation = 0
        self._baseline = 0
        self._accepted_generation = 0
        self._accepted: Observation | None = None
        self._watched: Any = None
        self._proof_run: str | None = None
        self._trip_menu: tuple[str, str | None, int, str | None] | None = None
        self._picker: tuple[str, str | None, int, str | None, str] | None = None
        self._day_labels: tuple[str, dict[str, str]] | None = None
        self._searching = False
        self.scope_reason: str | None = None

    def _out_of_scope(self, reason: str, detail: dict[str, Any] | None = None) -> RunnerError:
        """Record which closed check failed; the public failure code is unchanged."""
        if reason not in SCOPE_REASONS:
            raise AssertionError(reason)
        self.scope_reason = reason
        record: dict[str, Any] = {"phase": "scope_check", "outcome": "rejected", "reason": reason}
        if detail is not None:
            record["detail"] = detail
        self._diagnostic(record)
        return RunnerError("pilot_out_of_scope")

    def _preflight(self, run: _Run, *, capacity: str = "operation") -> None:
        self._arm(run)
        expected = pilot_request(run.request.limits.max_seconds)
        # The policy is a trusted entry-point option, never a request/DOM grant.
        fields = ("goal", "start_url", "mode", "values", "checks", "visual")
        if any(getattr(run.request, field) != getattr(expected, field) for field in fields):
            raise self._out_of_scope("request_mismatch")
        if (run.request.limits.max_seconds > 600
                or run.request.limits.max_steps > 40 or run.request.limits.max_provider_calls > 40
                or run.request.limits.max_input_bytes > 512*1024):
            raise self._out_of_scope("limits_exceed_pilot")
        if run.queued or run.dom is None or run.alpha is None:
            raise self._out_of_scope("run_not_ready")
        if (self._admission.get_proposal(run.alpha.proposal_id) is not run.alpha
                or not self._admission.task_scope_allows(run.result.run_id, "navigate", ORIGIN, ORIGIN)):
            raise self._out_of_scope("admission_scope")
        if self._proof_run not in {None, run.result.run_id}:
            raise self._out_of_scope("proof_run_mismatch")
        self._proof_run = run.result.run_id
        try:
            self.ledger.assert_active()
        except ExperimentBudgetError as exc:
            raise RunnerError(exc.code) from None
        remaining = self.ledger.snapshot
        usage, limits = run.budget.usage(), run.request.limits
        if capacity not in {"read", "call", "step", "operation"}:
            raise ValueError("invalid_pilot_capacity")
        # Capacity is needed before NEW work only. Auth/time/document checks
        # above still apply when using a paid response or reading final evidence.
        if ((capacity in {"call", "operation"} and (
                usage.calls >= limits.max_provider_calls or usage.input_bytes >= limits.max_input_bytes
                or remaining["remaining_calls"] <= 0 or remaining["remaining_input_bytes"] <= 0))
                or (capacity in {"step", "operation"} and (
                    usage.steps >= limits.max_steps or remaining["remaining_steps"] <= 0))):
            raise self._out_of_scope("budget_exhausted")

    def _watch(self, run: _Run) -> None:
        page = run.driver.page
        if self._watched is None:
            self._watched = page
            page.on("framenavigated", self._navigated)
        elif self._watched is not page:
            raise RunnerError("navigation")

    def _navigated(self, frame: Any) -> None:
        if self._watched is not None and frame is self._watched.main_frame:
            self._generation += 1

    async def _prove(self, run: _Run, obs: Observation, *, continuing: bool = False) -> None:
        self._preflight(run, capacity="read")
        if run.dom is None:
            raise RunnerError("blocked")
        generation = self._generation
        if run.driver.page.url != obs.url or not allowed_url(obs.url, results=self.search_action is not None):
            raise RunnerError("navigation")
        if obs.unsupported and any(reason not in {"iframe", "canvas", "shadow", "file", "download", "newwindow", "namedtarget"}
                                   for reason in obs.unsupported):
            raise RunnerError("blocked")
        proof = await self._wait(run, run.dom.prepare("wait", value=0))
        if (await self._wait(run, run.dom.guard(proof))).status != "pass":
            raise RunnerError("navigation")
        value = await self._wait(run, run.dom._world.evaluate(STATE_SCRIPT))
        labels = await self._read_day_labels(run, obs) if isinstance(value, dict) and value.get("calendar") is True else {}
        if ((await self._wait(run, run.dom.guard(proof))).status != "pass"
                or self._generation != generation or run.driver.page.url != obs.url):
            raise RunnerError("navigation")
        self._day_labels = (obs.id, labels)
        self._preflight(run, capacity="read")
        try:
            self._state = validate_state(value, obs.url or "", self._committed,
                                         search=self.search_action is not None, continuing=continuing,
                                         trip_menu_expected=self._trip_menu is not None and self._trip_menu == (
                                             obs.document_id, obs.url, generation, self._completed),
                                         picker_expected=self._picker[4] if self._picker is not None
                                         and self._picker[:4] == (obs.document_id, obs.url, generation,
                                                                  self._completed) else None)
        except PilotStateError as exc:
            raise self._out_of_scope(exc.reason, {"state": state_shape(value)}) from None
        # Validate every retained destination before any provider bytes, including
        # controls later excluded by phase. Full semantics remain in the bindings.
        self._questions(run, obs)
        self._accepted, self._accepted_generation = obs, generation
        self._baseline = generation

    async def _read_day_labels(self, run: _Run, obs: Observation) -> dict[str, str]:
        """Full dates of bare-number calendar day buttons, bound to this observation's tokens."""
        assert run.dom is not None
        days = [(token, candidate.semantic_by_operation.get("click", {}).get("node") or {})
                for token, candidate in obs.candidates.items()]
        days = [(token, node) for token, node in days if node.get("role") in {"button", "gridcell"}
                and re.fullmatch(r"[0-9]{1,2}(?: \S+)?", str(node.get("fullName", node.get("name", ""))))]
        labels: dict[str, str] = {}
        for token, node in days[:80]:
            handle = run.dom._handles.get(token)
            if handle is None:
                continue
            label = day_label(str(node.get("fullName", node.get("name"))),
                              await self._wait(run, handle.evaluate(DAY_LABEL_SCRIPT)))
            if label is not None:
                labels[token] = label
        return labels

    def _canonical(self, operation: str, node: dict[str, Any], token: object, obs_id: object) -> str | None:
        phase = self._state.get("phase")
        if (phase == "calendar" and self._day_labels is not None and self._day_labels[0] == obs_id
                and isinstance(token, str) and token in self._day_labels[1]):
            label = self._day_labels[1][token]
            node = {**node, "name": label, "fullName": label}
        return canonical_control(operation, node, phase)

    async def _continue(self, run: _Run, prior: Observation) -> Observation:
        self._preflight(run, capacity="read")
        attempt = self._completed
        if attempt is None or attempt in self._spent or self.continuations >= 4:
            raise RunnerError("navigation")
        # Spend synchronously before even observation; failures/cancellation never refund.
        self._spent.add(attempt)
        self.continuations += 1
        generation = self._generation
        if run.driver.page.url == prior.url:
            raise RunnerError("navigation")
        if run.dom is None:
            raise RunnerError("blocked")
        fresh = await self._wait(run, run.dom.observe())
        if fresh.document_id != prior.document_id or self._generation != generation:
            raise RunnerError("navigation")
        await self._prove(run, fresh, continuing=True)
        if self._generation != generation:
            raise RunnerError("navigation")
        run.current_observation = fresh
        self._diagnostic({"phase": "state_continuation", "outcome": "validated",
                          "continuations": self.continuations, "ownership": "unattributed"})
        return fresh

    def _diagnostic(self, record: dict[str, Any]) -> None:
        record["acknowledged_actions"] = sum(row.get("acknowledged") is True for row in self.trace)
        self.diagnostics.append(record)
        del self.diagnostics[:-40]

    async def _choose(self, run: _Run, obs: Observation) -> Action | None:
        record: dict[str, Any] = {"phase": "choose", "outcome": "failed",
                                  "url_matches_before": _url_matches(run, obs)}
        try:
            if self.continuation:
                self._preflight(run, capacity="call")
                self._watch(run)
                if run.driver.page.url != obs.url or self._generation != self._baseline:
                    raise RunnerError("stale")
                await self._prove(run, obs)
            action = await super()._choose(run, obs)
            if self.continuation:
                self._preflight(run, capacity="read")
                if run.driver.page.url != obs.url or self._generation != self._baseline:
                    raise RunnerError("stale")
            record["outcome"] = "done" if action is None else "chosen"
            return action
        except RunnerError as exc:
            if exc.code == "stale":
                record["outcome"] = "stale"
            raise
        finally:
            record["url_matches_after"] = _url_matches(run, obs)
            self._diagnostic(record)

    async def _refresh_stale(self, run: _Run, prior: Observation) -> Observation:
        last = self.diagnostics[-1] if self.diagnostics else {}
        trigger = None
        if last.get("outcome") == "stale":
            trigger = "choose" if last.get("phase") == "choose" else (
                "dispatch" if last.get("phase") == "dispatch" else None)
        record: dict[str, Any] = {"phase": "stale_refresh", "outcome": "failed",
                                  "trigger": trigger, "reason": None,
                                  "url_matches_before": _url_matches(run, prior)}
        try:
            if self.continuation:
                self._preflight(run, capacity="read")
                accepted = self._accepted
                if (accepted is not None and accepted is not prior
                        and accepted.document_id == prior.document_id
                        and run.driver.page.url == accepted.url
                        and self._generation == self._accepted_generation):
                    fresh = accepted  # synchronization only: no fresh capture/grant
                elif run.driver.page.url != prior.url or self._generation != self._baseline:
                    fresh = await self._continue(run, prior)
                else:
                    fresh = await super()._refresh_stale(run, prior)
            else:
                fresh = await super()._refresh_stale(run, prior)
            record["outcome"] = "refreshed"
            return fresh
        except RunnerError as exc:
            if exc.code == "navigation":
                known = {"stale_refresh:document_changed", "stale_refresh:observation_url_changed",
                         "stale_refresh:live_page_url_changed"}
                record["reason"] = next((item for item in run.result.evidence
                                         if type(item) is str and item in known), None)
            raise
        finally:
            record["url_matches_after"] = _url_matches(run, prior)
            self._diagnostic(record)

    def _apply(self, run: _Run, exc: BaseException) -> None:
        self.failure_code = safe_failure(exc)
        super()._apply(run, exc)

    def _questions(self, run: _Run, obs: Observation) -> tuple[
        tuple[ChoiceQuestion, ...], dict[str, str], dict[str, dict[str, dict[str, Any]]]
    ]:
        questions, heads, bindings = super()._questions(run, obs)
        # A finite target needs a bound parameter; there is no hidden generator.
        allowed = {group: {key: item for key, item in bag.items()
                          if item["operation"] != "fill" or item["name"] in run.request.values}
                   for group, bag in bindings.items()}
        allowed = {group: bag for group, bag in allowed.items() if bag}
        heads = {group: head for group, head in heads.items() if group in allowed}
        instructions = {question.name: question.instructions for question in questions}
        if self.continuation:
            seen: set[tuple[str, str]] = set()
            filtered: dict[str, dict[str, dict[str, Any]]] = {}
            inventory: dict[str, dict[str, dict[str, Any]]] = {}
            phase = self._state.get("phase")
            for group, bag in bindings.items():
                for key, item in bag.items():
                    op, semantic = item["operation"], item["semantic"]
                    node = semantic.get("node") or {}
                    name = self._canonical(op, node, item["token"], obs.id)
                    if op in {"scroll", "wait"}:
                        name = item["name"] if (op == "wait" or phase != "results") else None
                    if name is None:
                        continue
                    if node:
                        if semantic.get("url") != obs.url or semantic.get("origin") != ORIGIN:
                            raise RunnerError("navigation")
                        for dest in (node.get("href"), (semantic.get("form") or {}).get("action"), item.get("destination")):
                            if dest is not None and not allowed_url(dest, results=self.search_action is not None):
                                raise self._out_of_scope("unsafe_destination", {"control": name, "phase": phase})
                        candidate = obs.candidates.get(item["token"])
                        geometry = candidate.geometry if candidate is not None else None
                        category = "trip" if name in {"Round trip", "One way"} and node.get("role") in {"button", "combobox"} else name
                        if category in {"trip", "Where from?", "Where to?", "Departure"} and geometry and geometry.get("visible") is True:
                            inventory.setdefault(category, {})[item["token"]] = node
                        if (not geometry or geometry.get("visible") is not True or geometry.get("occluded") is True
                                or (node.get("state") or {}).get("disabled") is True):
                            continue
                    offered = (op in {"wait", "scroll"} and phase != "trip" or
                               phase == "form" and (name in {"Round trip", "One way"} and node.get("role") in {"combobox", "button"} and "One way" not in self._committed
                                   or name == "Where from?" and JFK not in self._committed
                                   or name == "Where to?" and LAX not in self._committed
                                   or name == "Departure" and DATE_DONE not in self._committed
                                   or name in {"Search", "Search for flights"} and self._state.get("ready")) or
                               phase == "trip" and name == "One way" and node.get("role") in {"option", "menuitemradio"} or
                               phase == "origin" and name in {"Where from?", JFK} or
                               phase == "destination" and name in {"Where to?", LAX} or
                               phase == "calendar" and name in {"Next month", "Previous month", DATE_LABEL, DATE_DONE})
                    if phase == "results":
                        offered = op == "wait"
                    if not offered:
                        continue
                    if (phase in {"origin", "destination"} and name in {"Where from?", "Where to?"}
                            and ((node.get("state") or {}).get("aria") or {}).get("expanded") != "true"):
                        continue
                    identity = (op, name)
                    if identity in seen:
                        raise self._out_of_scope("duplicate_offered_control", {"control": name, "phase": phase})
                    seen.add(identity)
                    if name == DATE_LABEL and node.get("name") != name:
                        # Provider context carries the bound full date, never the bare number.
                        item = {**item, "semantic": {**item["semantic"], "node": {
                            **node, "name": name}}}
                    filtered.setdefault(group, {})[key] = {**item, "name": name}
            allowed = filtered
            hidden_trip = phase == "trip" and self._state.get("form_observed") is False
            if hidden_trip and (seen != {("click", "One way")} or "One way" in self._committed):
                raise self._out_of_scope("hidden_trip_menu_mismatch", {"offered": len(seen)})
            # With the form hidden by a picker, only that picker's own field may be visible.
            hidden_picker = phase in PICKERS and self._state.get("form_observed") is False
            own = {"origin": "Where from?", "destination": "Where to?", "calendar": "Departure"}.get(str(phase))
            for category in (() if hidden_trip else ("trip", "Where from?", "Where to?", "Departure")):
                nodes = list(inventory.get(category, {}).values())
                expanded = [node for node in nodes if ((node.get("state") or {}).get("aria") or {}).get("expanded") == "true"]
                picker = {"Where from?": "origin", "Where to?": "destination"}.get(category)
                if hidden_picker:
                    if category == own:
                        valid = len(nodes) == 1 and (phase == "calendar" or len(expanded) == 1
                                                     and (nodes[0].get("state") or {}).get("tag") == "input")
                    else:
                        valid = len(nodes) <= 1 if category == "trip" and phase == "calendar" else not nodes
                else:
                    valid = len(nodes) == 1 or (picker is not None and phase == picker and len(nodes) == 2
                                                and len(expanded) == 1
                                                and all((node.get("state") or {}).get("tag") == "input" for node in nodes))
                if not valid:
                    reason = {"trip": "control_count_trip", "Where from?": "control_count_origin",
                              "Where to?": "control_count_destination"}.get(category, "control_count_departure")
                    raise self._out_of_scope(reason, {
                        "phase": phase, "visible": len(nodes), "expanded": len(expanded),
                        "inputs": sum((node.get("state") or {}).get("tag") == "input" for node in nodes)})
            heads = {group: f"target_{group}" for group in allowed}
        return ((ChoiceQuestion("operation", instructions["operation"], ("DONE", *heads)),
                 *(ChoiceQuestion(head, instructions[head], tuple(allowed[group]))
                   for group, head in heads.items())), heads, allowed)

    async def _snapshot(self, run: _Run) -> dict[str, Any]:
        if run.dom is None:
            raise RunnerError("blocked")
        if self.continuation:
            self._preflight(run, capacity="read")
            current = run.current_observation or run.dom.current_observation
            if current is None:
                raise RunnerError("navigation")
            if run.driver.page.url != current.url or self._generation != self._baseline:
                current = await self._continue(run, current)
            await self._prove(run, current)
        # A read cannot adopt a late SPA transition or replacement Document.
        # Preparing/guarding this document action has no dispatch or budget effect.
        proof = await self._wait(run, run.dom.prepare("wait", value=0))
        generation = self._generation
        if (await self._wait(run, run.dom.guard(proof))).status != "pass":
            raise RunnerError("navigation")
        value = await self._wait(run, run.dom._world.evaluate(FLIGHTS_EVIDENCE_SCRIPT))
        if (await self._wait(run, run.dom.guard(proof))).status != "pass":
            raise RunnerError("navigation")
        if not isinstance(value, dict):
            raise RunnerError("invalid_flights_evidence")
        if self.continuation:
            self._preflight(run, capacity="read")
            if self._generation != generation:
                raise RunnerError("navigation")
        return value

    async def _dispatch_one(self, run: _Run, obs: Observation, action: Action) -> Observation:
        if self.continuation:
            self._preflight(run, capacity="step")
            if run.driver.page.url != obs.url or self._generation != self._baseline:
                raise RunnerError("stale")
        semantic = json.loads(action.fingerprint).get("semantic", {})
        node = semantic.get("node") or {}
        full_name = str(node.get("fullName", node.get("name", "")))
        record = action_record(action)
        if self.continuation:
            record["name"] = self._canonical(action.operation, node, action.token, obs.id) or ""
        record["acknowledged"] = False
        if re.search(r"select flight|\bbook\b|sign in|track prices|captcha", full_name, re.IGNORECASE):
            raise self._out_of_scope("forbidden_control_name")
        # Reserve before an attempt: stale/unknown attempts are not refunded.
        try:
            self.ledger.reserve_step()
        except ExperimentBudgetError as exc:
            raise RunnerError(exc.code) from None
        search = action.operation in {"click", "submit"} and record["name"] in {
            "Search", "Search for flights"
        }
        pre = await self._snapshot(run) if search else None
        self._searching = search
        dispatched_at = time.time() * 1000
        record.update(outcome="prepared", dispatched_at_ms=dispatched_at)
        self.trace.append(record)
        prior_attempt = run.recent_actions[-1].get("attempt_id") if run.recent_actions else None
        dispatch_generation = self._generation
        try:
            try:
                result = await super()._dispatch_one(run, obs, action)
            finally:
                self._searching = False
            if self.continuation and result.url == obs.url and self._generation != dispatch_generation:
                # Acknowledged effect plus an observed leave/return has an unknown
                # postcondition. Keep its attempt and conservative journal outcome.
                try:
                    self._stamp(run, run.journal.post_observe_failure(
                        run.result.run_id, run.recent_actions[-1]["attempt_id"]))
                except Exception:  # noqa: BLE001 — retain unknown priority, never exception details
                    self._set(run, evidence=[*run.result.evidence, "journal_unavailable"][:32])
                raise RunnerError("outcome_unknown")
        except BaseException as exc:
            recent = run.recent_actions[-1] if run.recent_actions else {}
            acknowledged = bool(recent.get("attempt_id") and recent.get("attempt_id") != prior_attempt)
            record.update(acknowledged=acknowledged,
                          outcome="outcome_unknown" if run.queued or acknowledged else "not_dispatched",
                          attempt_id=run.attempt_id or (recent.get("attempt_id") if acknowledged else None))
            if self.continuation and (run.queued or acknowledged):
                self._completed = None
            self._diagnostic({"phase": "dispatch", "outcome": "stale" if (
                isinstance(exc, RunnerError) and exc.code == "stale") else "failed",
                "url_matches_after": _url_matches(run, obs)})
            raise
        record.update(acknowledged=True, outcome="acknowledged", attempt_id=run.recent_actions[-1]["attempt_id"],
                      acknowledged_at_ms=time.time()*1000)
        progress = run.recent_actions[-1].get("page_changed")
        record["page_changed"] = progress if type(progress) is bool else None
        same_url = _same_text(obs.url, result.url)
        record["post_observation"] = {
            "same_document": _same_text(obs.document_id, result.document_id),
            "same_origin": _same_text(obs.origin, result.origin),
            "url_changed": not same_url if same_url is not None else None,
            "live_url_matches": _url_matches(run, result),
        }
        if search and pre is not None:
            self.pre_search = pre
            self.search_action = {**record, "pre_search_digest": evidence_digest(pre)}
        elif action.operation == "click" and record["name"].startswith("Done."):
            self.date_selection = record
        if self.continuation:
            phase = self._state.get("phase")
            name = self._canonical(action.operation, node, action.token, obs.id)
            commits = name in COMMIT_QUERY and not (
                name == "One way" and node.get("role") not in {"option", "menuitemradio"})
            if commits and name is not None:
                self._committed = self._committed | {name}
            if action.operation not in {"wait", "screenshot"}:
                self._completed = run.recent_actions[-1]["attempt_id"]
                same = self._accepted is obs and result.document_id == obs.document_id and result.url == obs.url
                self._trip_menu = ((obs.document_id, obs.url, self._generation, self._completed)
                    if name in {"Round trip", "One way"} and node.get("role") in {"combobox", "button"}
                    and phase == "form" and same else None)
                # A confirmed field click opens its picker; actions inside it keep the
                # binding until the exact suggestion or Done closes it. Google may rewrite
                # the opaque query during the click itself, so the binding is to the
                # dispatch's own same-Document post-observation URL; later drift stops.
                opened = ({"Where from?": "origin", "Where to?": "destination", "Departure": "calendar"}.get(str(name))
                          if action.operation == "click" and phase == "form" else
                          phase if phase in PICKERS and name not in {JFK, LAX, DATE_DONE} else None)
                self._picker = ((obs.document_id, result.url, self._generation, self._completed, str(opened))
                                if opened is not None and self._accepted is obs
                                and result.document_id == obs.document_id and allowed_url(result.url) else None)
                if commits and self._generation == dispatch_generation:
                    # Google rewrites the opaque query ~0.3-0.5 s after a committing choice
                    # (observed 2026-10-11). Let it land on the closed form, where the
                    # ordinary continuation validates it, not inside the next picker.
                    # No provider call, step or refund; the original deadline still applies.
                    settle = time.monotonic() + COMMIT_SETTLE_SECONDS
                    while self._generation == dispatch_generation and time.monotonic() < settle:
                        await self._wait(run, asyncio.sleep(.05))
            self._baseline = self._generation
        return result

    async def _settle(self, run: _Run, dom: Any, action: Action) -> None:
        await super()._settle(run, dom, action)
        if not self._searching:
            return
        # Google tears down the form and fills rows over ~1-2 s after Search
        # (observed 2026-10-11); a 200 ms post-observation catches the old form
        # half-hidden. Read-only and bounded: no provider call, step or refund,
        # the original deadline applies, and the ordinary proofs still follow.
        start = time.monotonic()
        previous, outcome = None, "timeout"
        while time.monotonic() - start < RESULTS_SETTLE_SECONDS:
            try:
                value = await self._wait(run, dom._world.evaluate(FLIGHTS_EVIDENCE_SCRIPT))
                current = json.dumps([value["form"], value["results"], value["hazards"], value["truncated"]],
                                     sort_keys=True)
                rows = value["results"]["rows"] and not value["hazards"]["loading"]
            except (RunnerError, BudgetExceeded):
                raise
            except Exception:  # noqa: BLE001 — timing only; the observation that follows decides
                outcome = "unreadable"
                break
            if rows and current == previous:
                outcome = "settled"
                break
            previous = current if rows else None
            await self._wait(run, asyncio.sleep(.3))
        self._diagnostic({"phase": "results_settle", "outcome": outcome,
                          "elapsed_ms": round((time.monotonic() - start) * 1000)})

    async def _verify(self, run: _Run, finish: bool = False, due_to_quota: bool = False) -> bool:
        if self.search_action is not None and self.pre_search is not None:
            previous = await self._snapshot(run)
            await self._wait(run, asyncio.sleep(.3))
            current = await self._snapshot(run)
            self.report = validate_flights_evidence(
                self.pre_search, current, self.search_action,
                previous_post_search=previous, date_selection=self.date_selection,
            )
            if self.report.get("status") == "PASS":
                self._set(run, terminal="verified", reason="verified", functional="verified",
                          evidence=["flights:query_bound_visible_priced_result"])
                return True
        if finish:
            self._set(run, terminal="limit_exceeded" if due_to_quota else "verification_failed",
                      reason="budget_limit" if due_to_quota else "verification_failed",
                      functional="unverified")
        return False


def source_provenance() -> dict[str, Any]:
    root = Path(__file__).parent
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.iterdir()) if path.suffix in {".py", ".js"} and path.is_file()}
    return {"files": files, "sha256": hashlib.sha256(
        json.dumps(files, sort_keys=True).encode()).hexdigest()}


def export_journal(run: _Run) -> list[dict[str, Any]]:
    events = []
    for artifact_id in run.result.artifact_ids:
        artifact = run.store.read(artifact_id)
        if artifact.media_type == "application/json":
            value = json.loads(artifact.content)
            if isinstance(value, dict) and "event" in value:
                events.append(value)
    return events


async def run_pilot(ledger: ExperimentLedger) -> dict[str, Any]:
    ledger.assert_active()
    if not query_is_current():
        return {"status": "NOT_RUN", "reason": "expired_query"}
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return {"status": "NOT_RUN", "reason": "missing_injected_api_key"}
    initial_source = source_provenance()
    provider = FlightsProvider(key, ledger)
    manager = FlightsRunManager(provider, ledger, continuation=True)
    report: dict[str, Any] = {}
    try:
        ledger.reserve_step()  # Initial navigation is an effect, including on a resumed attempt.
        run_id = manager.start_task(pilot_request(ledger.remaining_seconds))
        report["run_id"] = run_id
        run = manager._run
        if run is None or run.task is None:
            raise RunnerError("not_started")
        await run.task
        result = manager.get_task(run_id).model_dump(mode="json")
        report["result"] = result
        report["status"] = "PASS" if result["terminal"] == "verified" else "NOT_VERIFIED"
        report["journal"] = export_journal(run)
    except Exception as exc:  # noqa: BLE001 — retain only a closed code, never raw exception data
        report.update(status="NOT_VERIFIED", failure_code=safe_failure(exc))
    finally:
        # Try both closers independently. A secondary cleanup exception must not
        # erase a primary unknown outcome or the partial diagnostics already held.
        cleanup = []
        for closer in (manager.aclose, provider.aclose):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 — closed diagnostic only
                cleanup.append(safe_failure(exc))
        if cleanup:
            report.update(status="NOT_VERIFIED", cleanup_failures=cleanup)
    # Enrichment is secondary evidence, so failed filesystem reads must not
    # replace the already retained unknown outcome, actions and diagnostics.
    report_failures = []
    try:
        stable_source = initial_source == source_provenance()
    except Exception:  # noqa: BLE001 — source read failures are closed, secondary diagnostics
        stable_source = False
        report_failures.append("source_provenance_unavailable")
    try:
        experiment = ledger.snapshot
    except Exception:  # noqa: BLE001 — unavailable usage is explicit, never an invented zero
        experiment = None
        report_failures.append("experiment_snapshot_unavailable")
    if not stable_source or report_failures:
        report["status"] = "NOT_VERIFIED"
    if report_failures:
        report["report_failures"] = report_failures
    report.setdefault("failure_code", manager.failure_code)
    if manager.failure_code == "outcome_unknown":
        report["failure_code"] = "outcome_unknown"
    report.update(scope_reason=manager.scope_reason, evidence=manager.report,
                  actions=manager.trace, diagnostics=manager.diagnostics,
                  experiment=experiment, source=initial_source, source_stable=stable_source,
                  continuations=manager.continuations,
                  acceptance="production runner, Decisions only, isolated Chromium")
    return project_report(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args(argv)
    # Reject an overwrite before any browser or API work.
    with args.output.open("x", encoding="utf-8") as output:
        try:
            with ExperimentLedger(args.ledger, create=args.initialize) as ledger:
                report = asyncio.run(run_pilot(ledger))
                if report.get("status") == "PASS":
                    ledger.finish()
        except Exception as exc:  # noqa: BLE001 — sanitized boundary; never serialize secrets/errors
            report = {"status": "NOT_VERIFIED", "reason": safe_failure(exc)}
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"status": report.get("status"), "output": str(args.output)}))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

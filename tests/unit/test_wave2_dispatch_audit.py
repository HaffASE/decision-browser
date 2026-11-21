"""F05 quota/audit regressions; synthetic data and real in-process boundaries."""

import json

import pytest
from pydantic import ValidationError

from decision_browser.artifacts import ArtifactError, ArtifactStore, Journal
from decision_browser.budget import Budget, BudgetExceeded
from decision_browser.contracts import Limits, PrivacyPolicy, TaskRequest
from decision_browser.runner import RunManager


@pytest.mark.parametrize("field,operation", [
    ("max_steps", lambda b: b.reserve_step()),
    ("max_provider_calls", lambda b: b.reserve_provider(0)),
    ("max_input_bytes", lambda b: b.reserve_provider(1)),
    ("max_output_bytes", lambda b: b.consume_output(1)),
    ("max_stored_bytes", lambda b: b.reserve_stored(1)),
])
def test_caller_limits_mutation_cannot_raise_budget_quota(field, operation):
    limits = Limits(**{field: 1})
    budget = Budget(limits)
    operation(budget)
    setattr(limits, field, 1000)
    with pytest.raises(BudgetExceeded):
        operation(budget)


def test_runner_owns_validated_request_snapshot():
    request = TaskRequest(goal="Review", start_url="https://example.test/", values={},
                          checks=[], limits=Limits(max_steps=1), privacy=PrivacyPolicy())
    manager = RunManager(lambda: None)
    run = manager._open(request)
    request.limits.max_steps = 999
    request.values["Name"] = "changed"
    assert run.request.limits.max_steps == 1
    assert run.request.values == {}


def test_budget_validates_forged_limits_instead_of_copying_unchecked_fields():
    forged = Limits.model_copy(Limits(), update={"max_steps": 1001})
    with pytest.raises(ValidationError):
        Budget(forged)


def _journal(clock, *, cap=4096, count=128):
    budget = Budget(Limits(max_seconds=1.0, max_stored_bytes=cap), lambda: clock[0])
    store = ArtifactStore(cap, max_artifacts=count, reserve_bytes=budget.reserve_stored)
    return budget, store, Journal(store)


def test_reserved_outcome_survives_execution_deadline_without_extending_it():
    now = [0.0]
    budget, store, journal = _journal(now)
    reservation = journal.reserve_attempt("run", "digest", "semantic", "ordinary_admitted", budget=budget)
    attempt = journal.before_dispatch("run", "digest", "semantic", "ordinary_admitted")
    now[0] = budget.original_deadline
    outcome = journal.outcome("run", attempt, "outcome_unknown")
    reservation.release()
    assert json.loads(store.read(outcome).content)["status"] == "outcome_unknown"
    assert budget.original_deadline == 1.0
    assert budget.usage().stored_bytes == store.stored_bytes
    with pytest.raises(BudgetExceeded, match="deadline_expired"):
        budget.reserve_step()
    with pytest.raises(BudgetExceeded, match="deadline_expired"):
        store.put_text("ordinary artifact")


@pytest.mark.parametrize("resource", ["bytes", "count"])
def test_failed_attempt_reservation_releases_all_capacity(resource):
    now = [0.0]
    budget, store, journal = _journal(now, cap=64 if resource == "bytes" else 4096,
                                     count=1 if resource == "count" else 128)
    before = budget.usage()
    with pytest.raises((ArtifactError, BudgetExceeded)):
        journal.reserve_attempt("run", "digest", "semantic", "ordinary_admitted", budget=budget)
    assert budget.usage() == before and store.stored_bytes == 0
    assert len(store._items) == 0
    store.put_text("safe")
    assert budget.usage().stored_bytes == store.stored_bytes == 4


def test_unused_attempt_reservation_releases_step_and_artifact_capacity():
    now = [0.0]
    budget, store, journal = _journal(now, count=4)
    reservation = journal.reserve_attempt("run", "digest", "semantic", "ordinary_admitted", budget=budget)
    reservation.release()
    assert budget.usage().steps == budget.usage().stored_bytes == 0
    for _ in range(4):
        store.put_text("safe")
    assert budget.usage().stored_bytes == store.stored_bytes == 16


def test_reserved_audit_has_fixed_bounded_window():
    now = [0.0]
    budget, store, journal = _journal(now)
    reservation = journal.reserve_attempt("run", "digest", "semantic", "ordinary_admitted", budget=budget)
    attempt = journal.before_dispatch("run", "digest", "semantic", "ordinary_admitted")
    now[0] = budget.original_deadline + 2.0
    with pytest.raises(BudgetExceeded, match="audit_deadline_expired"):
        journal.outcome("run", attempt, "outcome_unknown")
    reservation.release()
    assert len(store._items) == 1

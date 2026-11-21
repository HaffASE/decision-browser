"""Closed diagnostic codes and bounded public evidence composition.

Page text, exception messages and arbitrary metadata never define new codes.
When both channels fill, lifecycle diagnostics and check evidence each retain
at least sixteen available slots; either can use all thirty-two when alone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

_CAP = 32
_CHANNEL_RESERVE = 16
_CHECK_KINDS = frozenset({"route", "text", "value", "visible", "count", "fixture_state"})

_PROVIDER = frozenset({
    "answer_name", "choice_count", "confidence", "duplicate_choices",
    "duplicate_question_names", "duplicates", "empty_choices", "extra_answer",
    "http_error", "incomplete", "incomplete_distribution", "invalid_answer",
    "invalid_answers", "invalid_api_key", "invalid_choice", "invalid_choices",
    "invalid_client", "invalid_context", "invalid_descriptions", "invalid_input",
    "invalid_instructions", "invalid_mapping", "invalid_model", "invalid_name",
    "invalid_operation", "invalid_path", "invalid_provider_config", "invalid_request",
    "invalid_response", "invalid_usage", "malformed", "malformed_response",
    "max_text_chars", "missing_answer", "missing_target", "probabilities",
    "probability_range", "refusal", "refused", "rejected_sentinel", "response_close_failed",
    "schema", "text_provider_not_configured", "transport_error", "type_mismatch",
    "unknown_choice", "unsupported_content_encoding", "unsupported_question",
})
_RUNNER = frozenset({
    "bad_request", "blocked", "busy", "cancelled", "clock", "closed",
    "consequential_action_blocked", "expired", "failed", "helper_schema",
    "invalid_api_key", "invalid_provider_config", "limit_exceeded", "missing_api_key",
    "navigation", "no_supported_human_grant_channel", "no_progress", "budget_limit",
    "outcome_unknown", "presentation_busy", "presentation_resolved", "stale",
    "task_scope_isolated_only", "task_scope_expansion_required", "task_scope_origin_mismatch",
    "privacy_blocked", "refusal", "verification_failed", "unknown",
})
_ARTIFACT = frozenset({
    "attempt_active", "bad_attempt", "bad_blocked", "bad_bytes", "bad_event", "bad_id",
    "bad_max_artifacts", "bad_max_bytes", "bad_media", "bad_reservation", "bad_reserve",
    "bad_status", "bad_store", "bad_text", "blocked_bytes", "json_encode", "non_finite",
    "over_bytes", "over_count", "redaction_failed", "unknown_id", "unsupported_json",
})
_BUDGET = frozenset({
    "audit_deadline_expired", "deadline_expired", "input_bytes", "output_bytes",
    "provider_calls", "steps", "stored_bytes", "limit_exceeded",
})
_BROWSER = frozenset({
    "already_closed", "already_started", "cdp_connect_failed", "cleanup_uncertain",
    "existing_chrome_unsupported", "context_count", "devtools_port_malformed", "devtools_port_too_large",
    "devtools_port_unreadable", "document_read_denied", "endpoint_rejected",
    "invalid_devtools_path", "invalid_mode", "invalid_timeout", "inventory_failed",
    "launch_failed", "not_started", "owned_page_failed", "ownership_unverified",
    "screenshot_failed",
})
_FAMILIES = {
    "ProviderError": ("provider", _PROVIDER),
    "CodecError": ("provider", _PROVIDER),
    "ProviderConfigError": ("provider", frozenset({"invalid_provider_config"})),
    "RunnerError": ("runner", _RUNNER),
    "ArtifactError": ("artifact", _ARTIFACT),
    "BudgetExceeded": ("budget", _BUDGET),
    "BrowserBlocked": ("browser", _BROWSER),
    "DeskUnavailable": ("desk", frozenset({"blocked", "no_supported_human_grant_channel"})),
}
_SECONDARY = frozenset({
    "provider:response_close_failed", "provider:primary_output_bytes",
    "provider:primary_deadline_expired", "provider:primary_http_error",
    "provider:primary_malformed_response", "provider:primary_transport_error",
    "provider:primary_unsupported_content_encoding",
    "http_status:unauthorized", "http_status:rate_limited", "http_status:server_error",
    "http_status:client_error", "http_status:redirect",
})


def error_notes(error: BaseException) -> tuple[str, ...]:
    """Copy known producer tokens only, including bounded secondary failures."""
    notes: list[str] = []
    if isinstance(error, asyncio.CancelledError):
        notes.append("runner:cancelled")
    family = _FAMILIES.get(type(error).__name__)
    if family is not None:
        prefix, allowed = family
        code = getattr(error, "code", None)
        if not isinstance(code, str):
            code = getattr(error, "reason", None)
        if type(code) is str and len(code) <= 64 and code in allowed:
            notes.append(prefix + ":" + code)
    extra = getattr(error, "diagnostics", ())
    if type(extra) is tuple:
        for item in extra[:16]:
            if type(item) is str and len(item) <= 64 and item in _SECONDARY and item not in notes:
                notes.append(item)
    return tuple(notes)


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def status_note(reason: object) -> str | None:
    """Keep a known failure status before cleanup replaces the public reason."""
    if type(reason) is str and len(reason) <= 64 and reason in _RUNNER:
        return "runner:" + reason
    return None


def merge_evidence(
    existing: Sequence[str], fresh: Sequence[str], *, replace_checks: bool = False,
) -> list[str]:
    """Keep fresh causes and preserve diagnostic space across verification."""
    def check(note: str) -> bool:
        return note.partition(":")[0] in _CHECK_KINDS

    retained = [note for note in existing if not replace_checks or not check(note)]
    merged = _unique([*fresh, *retained])
    lifecycle = [note for note in merged if not check(note)]
    checks = [note for note in merged if check(note)]
    lifecycle_cap = min(len(lifecycle), max(_CHANNEL_RESERVE, _CAP - len(checks)))
    return [*lifecycle[:lifecycle_cap], *checks[:_CAP - lifecycle_cap]]

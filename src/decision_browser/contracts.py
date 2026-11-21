"""Closed Decision Browser contracts.

Strict models reject unknown fields. A TaskResult does not grant authorization
or any effect. PrivacyPolicy.blocked_literals must never be sent or logged.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from decision_browser.check_validation import (
    EXPECTED_CHARS,
    MAX_CHECKS,
    TARGET_BYTES,
    CheckKind,
    check_problem,
)

_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

Mode = Literal["isolated", "existing_chrome"]
BROWSER_OPERATIONS = (
    "scroll", "wait", "screenshot", "navigate", "click", "fill", "select", "enter", "submit",
)
Terminal = Literal[
    "queued",
    "awaiting_human_admission",
    "running",
    "verified",
    "completed_unverified",
    "verification_failed",
    "blocked",
    "cancelled",
    "limit_exceeded",
    "outcome_unknown",
    "failed",
    "refused",
    "unsupported",
]
Functional = Literal["not_run", "verified", "unverified", "failed"]
VisualDiagnostic = Literal[
    "not_requested", "not_run", "complete", "incomplete", "failed"
]
AdmissionClass = Literal[
    "fixture_mapped",
    "observe_driver",
    "ordinary_admitted",
    "ordinary_task_scoped",
    "ordinary_beta_admitted",
    "consequential",
    "unresolved",
    "owned_loopback_document",
]


def _parse_expected(value: object) -> str | bool | int:
    if type(value) is bool or type(value) is int:
        return value
    if type(value) is str and len(value) <= EXPECTED_CHARS:
        return value
    raise ValueError("expected must be str (<=4000), bool, or int")


Expected = Annotated[str | bool | int, BeforeValidator(_parse_expected)]


def _unsafe_url_text(value: str) -> bool:
    return any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value)


def _http_url(value: object, *, allow_query: bool, allow_fragment: bool) -> str:
    if type(value) is not str or _unsafe_url_text(value):
        raise ValueError("URL must be http(s) without whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must be absolute http(s) with a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL must not include userinfo")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("URL port must be 1..65535") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port must be 1..65535")
    if not allow_query and ("?" in value or parts.query != ""):
        raise ValueError("URL must not include a query")
    if not allow_fragment and ("#" in value or parts.fragment != ""):
        raise ValueError("URL must not include a fragment")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Viewport(StrictModel):
    width: int = Field(ge=100, le=4096)
    height: int = Field(ge=100, le=4096)


class Check(StrictModel):
    kind: CheckKind
    target: str = Field(default="", max_length=TARGET_BYTES)
    expected: Expected

    @model_validator(mode="after")
    def _check_fields(self) -> Check:
        problem = check_problem(self.kind, self.target, self.expected)
        if problem is not None:
            raise ValueError(problem)
        return self


class Limits(StrictModel):
    max_steps: int = Field(default=12, gt=0, le=1000)
    max_seconds: float = Field(default=120.0, gt=0, le=600)
    max_provider_calls: int = Field(default=12, gt=0, le=1000)
    max_input_bytes: int = Field(default=262144, gt=0, le=16777216)
    max_output_bytes: int = Field(default=262144, gt=0, le=16777216)
    max_stored_bytes: int = Field(default=10485760, gt=0, le=104857600)

    @field_validator("max_seconds")
    @classmethod
    def _finite_seconds(cls, value: float) -> float:
        if type(value) is not float or not math.isfinite(value):
            raise ValueError("max_seconds must be a finite float")
        return value


class PrivacyPolicy(StrictModel):
    """Local filter. Do not send or log blocked_literals."""

    blocked_literals: list[str] = Field(default_factory=list, max_length=32)
    send_images: bool = False

    @field_validator("blocked_literals")
    @classmethod
    def _literals(cls, value: list[str]) -> list[str]:
        for item in value:
            if type(item) is not str or len(item) == 0 or len(item) > 256:
                raise ValueError("each blocked literal must be 1..256 characters")
        return value


class UsageCounters(StrictModel):
    calls: int = Field(ge=0)
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    steps: int = Field(ge=0)
    stored_bytes: int = Field(ge=0)


def _default_viewports() -> list[Viewport]:
    return [
        Viewport(width=390, height=844),
        Viewport(width=768, height=1024),
        Viewport(width=1440, height=900),
    ]


class TaskRequest(StrictModel):
    goal: str = Field(min_length=1, max_length=4000)
    start_url: str
    mode: Mode = "isolated"
    values: dict[str, str]
    checks: list[Check] = Field(max_length=MAX_CHECKS)
    viewports: list[Viewport] = Field(
        default_factory=_default_viewports, min_length=1, max_length=3
    )
    limits: Limits
    privacy: PrivacyPolicy
    visual: bool = False

    @field_validator("goal", mode="before")
    @classmethod
    def _goal(cls, value: object) -> object:
        if type(value) is str:
            return value.strip()
        return value

    @field_validator("start_url")
    @classmethod
    def _start_url(cls, value: str) -> str:
        return _http_url(value, allow_query=True, allow_fragment=True)

    @field_validator("values")
    @classmethod
    def _values(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("at most 32 values")
        for key, item in value.items():
            if type(key) is not str or len(key) > 128:
                raise ValueError("value keys must be strings of at most 128 characters")
            if type(item) is not str or len(item) > 4000:
                raise ValueError(
                    "value entries must be strings of at most 4000 characters"
                )
        return value

    @field_validator("checks")
    @classmethod
    def _checks(cls, value: list[Check]) -> list[Check]:
        if len(value) > MAX_CHECKS:
            raise ValueError("at most 32 checks")
        for check in value:
            problem = check_problem(check.kind, check.target, check.expected)
            if problem is not None:
                raise ValueError(problem)
            if check.kind == "fixture_state":
                raise ValueError("fixture_state_requires_trusted_adapter")
        return value


class TaskResult(StrictModel):
    """Closed outcome. Recording admission does not authorize an effect."""

    run_id: str = Field(min_length=1)
    terminal: Terminal
    functional: Functional = "not_run"
    visual_diagnostic: VisualDiagnostic = "not_requested"
    usage: UsageCounters
    timings: dict[str, float]
    artifact_ids: list[str]
    reason: str | None = None
    recovery: str | None = None
    admission_class: AdmissionClass = "unresolved"
    effect_guarantee: Literal["none"] = "none"
    development_only: bool = False
    last_url: str | None = None
    evidence: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("timings")
    @classmethod
    def _timings(cls, value: dict[str, float]) -> dict[str, float]:
        if len(value) > 16:
            raise ValueError("at most 16 timings")
        for key, item in value.items():
            if type(key) is not str or len(key) > 64:
                raise ValueError(
                    "timing names must be strings of at most 64 characters"
                )
            if type(item) is not float or not math.isfinite(item) or item < 0:
                raise ValueError("timing values must be finite floats >= 0")
        return value

    @field_validator("artifact_ids")
    @classmethod
    def _artifact_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if type(item) is not str or _ARTIFACT_ID.fullmatch(item) is None:
                raise ValueError("artifact_ids must be safe ids, not paths")
        return value

    @field_validator("last_url")
    @classmethod
    def _last_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _http_url(value, allow_query=False, allow_fragment=False)

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: list[str]) -> list[str]:
        if len(value) > 32:
            raise ValueError("at most 32 evidence entries")
        for item in value:
            if type(item) is not str or len(item) > 4096:
                raise ValueError(
                    "evidence entries must be strings of at most 4096 characters"
                )
        return value

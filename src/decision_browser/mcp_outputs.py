"""Descriptions of existing structured tool results; no envelope conversion.

The pinned SDK validates successful CallToolResult.structured_content against
these types and returns the original CallToolResult unchanged. Error results
retain isError and the existing {"error": code} body instead of this schema.
"""

from typing import Annotated, Any, Literal, NotRequired

from pydantic import Field
from typing_extensions import TypedDict

from decision_browser.contracts import TaskResult


class PendingProposal(TypedDict):
    """Copy these identifiers verbatim; the remaining scope fields depend on proposal kind."""

    run_id: str
    proposal_id: str
    digest: str
    expires_at: float
    deadline: float
    development_only: bool
    budgets: NotRequired[dict[str, Any]]
    canonical_urls: NotRequired[list[str]]
    operations: NotRequired[list[str]]
    origins: NotRequired[list[str]]
    residual_risk: NotRequired[str]
    destination_origin: NotRequired[str]
    document_origin: NotRequired[str]
    semantic_fingerprint: NotRequired[str]
    operation: NotRequired[str]
    destination_url: NotRequired[str]


class TaskSnapshot(TypedDict):
    """Current run snapshot; queued/awaiting_human_admission/running need further polling."""

    result: Annotated[TaskResult, Field(description="Run ID, state, usage, evidence and artifact IDs. A result is not authorization.")]
    pending_proposal: Annotated[PendingProposal | None, Field(description="Current admission scope, or null while preparing/running/finished. Pass its exact identifiers to present_admission only when awaiting_human_admission.")]


class ArtifactBody(TypedDict):
    """Legacy whole artifact or explicit byte-range metadata; channel selects where base64 appears."""

    id: str
    media_type: str
    sha256: Annotated[str, Field(description="SHA-256 of the entire original artifact, not just this chunk.")]
    content: NotRequired[Annotated[str, Field(description="Base64 bytes for legacy/structured. Absent for resource: read the embedded resource blob.")]]
    format: NotRequired[Literal["decision-browser-artifact-range-v1"]]
    chunk_sha256: NotRequired[str]
    total_bytes: NotRequired[int]
    offset: NotRequired[int]
    next_offset: NotRequired[Annotated[int | None, Field(description="Next byte offset for explicit channels; null at EOF.")]]
    eof: NotRequired[bool]


class ArtifactReadCapabilities(TypedDict):
    format: str
    max_chunk_bytes: int
    default_channel: str
    channels: list[str]


class CapabilityFacts(TypedDict):
    """Local prerequisites and supported operations; no live or production certification."""

    modes: list[str]
    ops: list[str]
    classes: list[str]
    artifact_read: ArtifactReadCapabilities
    deferred_consequences: bool
    native_human: Literal["NOT_RUN"]
    live_ac10: Literal["NOT_RUN"]
    both_modes: Literal["NOT_RUN"]
    provider: str
    api_key_present: bool
    provider_ready: bool
    provider_error: str | None
    text_generation: str
    visual: str
    live_status: Literal["PREREQUISITES_PRESENT", "BLOCKED"]
    production_ready: Literal[False]
    admission_mode: Literal["standard_form", "task_scope"]
    human_grant_channel: Literal["standard_mcp_form"] | None
    form_candidate_unverified: bool

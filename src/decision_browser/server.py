import asyncio
import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    EmbeddedResource,
    FormElicitationCapability,
    TextContent,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from decision_browser import __version__
from decision_browser.admission import AdmissionStore
from decision_browser.artifacts import ARTIFACT_READ_MAX_BYTES, ArtifactError
from decision_browser.contracts import (
    BROWSER_OPERATIONS,
    AdmissionClass,
    TaskRequest,
    TaskResult,
)
from decision_browser.desk import DeskUnavailable, Proposal, describe
from decision_browser.mcp_compat import close_tool_arguments
from decision_browser.provider import OpenAIProvider
from decision_browser.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    provider_status,
)
from decision_browser.runner import AdmissionMode, ProviderPort, RunManager, RunnerError
from decision_browser.systemone_provider import SystemOneProvider

SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$", strict=True)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", strict=True)]
Offset = Annotated[int, Field(strict=True, ge=0)]
ReadLimit = Annotated[int, Field(strict=True, ge=1, le=ARTIFACT_READ_MAX_BYTES)]
MCPReply = Annotated[CallToolResult, dict[str, Any]]
_RANGE_FORMAT = "decision-browser-artifact-range-v1"

_mcp_ctx: ContextVar[Context[Any, Any] | None] = ContextVar(
    "decision_mcp_ctx", default=None
)
_NO_GRANT = "no_supported_human_grant_channel"


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reviewed: bool


class StandardFormDesk:
    """Standard MCP form channel. A host accept is not a native human click.

    Calls AdmissionStore.accept_standard_form only after the same pending object
    is still pending, non-development, fresh, and unrevoked, and only for action
    accept with strict reviewed True. Reviewed False and decline cancel that
    proposal. Form cancel revokes the run. No caller grant flag is consulted.
    """

    def __init__(self, store: AdmissionStore) -> None:
        if not isinstance(store, AdmissionStore):
            raise TypeError("store")
        self._store = store

    @property
    def development_only(self) -> bool:
        return False

    def _pending(self, proposal: Proposal) -> bool:
        return self._store.standard_form_pending(proposal)

    async def present(self, proposal: Proposal) -> bool:
        ctx = _mcp_ctx.get()
        try:
            if ctx is None:
                raise DeskUnavailable(_NO_GRANT)
            session = ctx.request_context.session
            form = ClientCapabilities(
                elicitation=ElicitationCapability(form=FormElicitationCapability())
            )
            capabilities = session.client_capabilities
            declared = capabilities.elicitation if isinstance(capabilities, ClientCapabilities) else None
            # Protocol 2025-11-25 keeps empty elicitation={} equivalent to
            # form={}; URL-only clients do not support a form. The pinned
            # SDK's generic capability predicate only checks elicitation exists.
            if (
                declared is None
                or (declared.form is None and declared.model_dump(exclude_none=True) != {})
                or not session.check_client_capability(form)
            ):
                raise DeskUnavailable(_NO_GRANT)
            if not self._pending(proposal):
                return False
            message = json.dumps(describe(proposal), sort_keys=True) + (
                " residual auto-save hidden mutation, known consequences"
                " excluded; standard MCP form is the admission channel;"
                " host accept is not proof of a native human click"
            )
            raw_schema = {
                "type": "object",
                "properties": {"reviewed": {
                    "type": "boolean",
                    "title": "I reviewed the displayed action scope",
                    "description": "Confirm the destinations, operations, limits and residual risks.",
                }},
                "required": ["reviewed"],
                "additionalProperties": False,
            }
            response = await session.elicit_form(
                message=message,
                requested_schema=raw_schema,
                related_request_id=ctx.request_id,
            )
            if not self._pending(proposal):
                return False
            action = response.action
            if action == "cancel":
                self._store.revoke(proposal.run_id)
                return False
            if action == "decline":
                self._store.cancel(proposal.proposal_id)
                return False
            if action != "accept":
                return False
            try:
                parsed = Confirm.model_validate(response.content)
            except ValidationError:
                return False
            if parsed.reviewed is False:
                if not self._pending(proposal):
                    return False
                self._store.cancel(proposal.proposal_id)
                return False
            if parsed.reviewed is not True:
                return False
            if not self._pending(proposal):
                return False
            return self._store.accept_standard_form(
                proposal.proposal_id, proposal.digest
            )
        except asyncio.CancelledError:
            raise
        except DeskUnavailable:
            raise
        except Exception:  # noqa: BLE001 - fail-closed MCP boundary
            raise DeskUnavailable(_NO_GRANT) from None


def production_factory() -> ProviderPort:
    try:
        settings = load_provider_settings()
    except ProviderConfigError as exc:
        raise RunnerError(exc.code) from None
    if settings.provider == "openai":
        api_key = settings.api_key
        if api_key is None:
            raise RunnerError("missing_api_key")
        return OpenAIProvider(api_key=api_key)
    if settings.provider != "local" and settings.api_key is None:
        raise RunnerError("missing_api_key")
    return SystemOneProvider(
        endpoint=settings.endpoint,
        model=settings.model,
        api_key=settings.api_key,
        max_choices=settings.max_choices,
        text=settings.text,
    )


def _result(
    manager: RunManager, run_id: str | None = None, *, result: TaskResult | None = None
) -> dict[str, Any]:
    if result is None:
        result = manager.get_task(run_id)
    pending = manager.pending_proposal(result.run_id)
    return {
        "result": result.model_dump(),
        "pending_proposal": describe(pending, development_only=result.development_only) if pending else None,
    }


def _reply(body: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        structured_content=body,
        is_error=error,
    )


def _error(exception: BaseException) -> CallToolResult:
    if isinstance(exception, ArtifactError):
        # Preserve the original sanitized legacy store-error code. bad_range
        # belongs to the newly introduced range API, not a renamed old error.
        return _reply({"error": "bad_range" if exception.code == "bad_range" else "failed"}, error=True)
    if isinstance(exception, (RunnerError, DeskUnavailable)):
        code = getattr(exception, "code", None) or getattr(exception, "reason", None)
        return _reply({"error": code if code else "failed"}, error=True)
    return _reply({"error": "failed"}, error=True)


def _guard(factory: Any) -> CallToolResult:
    try:
        body = factory()
        return body if isinstance(body, CallToolResult) else _reply(body)
    except Exception as exception:  # noqa: BLE001 - sanitized tool boundary
        return _error(exception)


def create_server(
    manager: RunManager | None = None, *, admission_mode: AdmissionMode = "standard_form"
) -> MCPServer[Any]:
    if admission_mode not in {"standard_form", "task_scope"}:
        raise ValueError("admission_mode")
    if manager is None:
        store = AdmissionStore(
            clock=time.monotonic, cleanup_grace=2.0,
            task_scope_enabled=admission_mode == "task_scope",
        )
        active: RunManager = RunManager(
            provider_factory=production_factory,
            admission_store=store,
            desk=StandardFormDesk(store),
            clock=time.monotonic,
            cleanup_grace=2.0,
            admission_mode=admission_mode,
        )
    else:
        active = manager

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await active.aclose()

    server: MCPServer[Any] = MCPServer(
        "decision-browser", version=__version__, log_level="WARNING", lifespan=lifespan
    )

    @server.tool()
    async def start_task(request: TaskRequest) -> MCPReply:
        """Start one task. Strict args. No automatic retry."""
        return _guard(lambda: _result(active, active.start_task(request)))

    @server.tool()
    async def get_task(run_id: SafeId | None = None) -> MCPReply:
        """Read one task. Strict args. Read only."""
        return _guard(lambda: _result(active, run_id))

    @server.tool()
    async def cancel_task(run_id: SafeId) -> MCPReply:
        """Cancel one task. Strict args."""
        return _guard(lambda: _result(active, result=active.cancel_task(run_id)))

    @server.tool()
    async def read_artifact(
        run_id: SafeId, artifact_id: SafeId, offset: Offset = 0,
        limit: ReadLimit = ARTIFACT_READ_MAX_BYTES,
        channel: Literal["legacy", "structured", "resource"] = "legacy",
    ) -> MCPReply:
        """Read bounded artifact bytes. Legacy small reads retain both envelopes; explicit channels page without duplicate base64."""

        def _do() -> CallToolResult:
            artifact = active.read_artifact_range(run_id, artifact_id, offset=offset, limit=limit)
            encoded = base64.b64encode(artifact.content).decode("ascii")
            if channel == "legacy":
                if offset != 0 or artifact.next_offset is not None:
                    raise RunnerError("artifact_range_required")
                return _reply({
                    "id": artifact.id, "media_type": artifact.media_type,
                    "content": encoded, "sha256": artifact.sha256,
                })
            metadata: dict[str, Any] = {
                "format": _RANGE_FORMAT,
                "id": artifact.id,
                "media_type": artifact.media_type,
                "sha256": artifact.sha256,
                "chunk_sha256": hashlib.sha256(artifact.content).hexdigest(),
                "total_bytes": artifact.total_bytes,
                "offset": artifact.offset,
                "next_offset": artifact.next_offset,
                "eof": artifact.next_offset is None,
            }
            if channel == "structured":
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(metadata))],
                    structured_content={**metadata, "content": encoded},
                )
            return CallToolResult(
                content=[EmbeddedResource(resource=BlobResourceContents(
                    uri=f"decision-browser-artifact://{run_id}/{artifact_id}?offset={offset}",
                    mime_type=artifact.media_type, blob=encoded,
                ))],
                structured_content=metadata,
            )

        return _guard(_do)

    @server.tool()
    async def capabilities() -> MCPReply:
        """Report capability facts. Strict args. Read only."""
        status = provider_status()
        return _guard(
            lambda: {
                "modes": (["isolated"] if active.admission_mode == "task_scope"
                          else ["isolated", "existing_chrome"]),
                "ops": list(BROWSER_OPERATIONS),
                "classes": list(get_args(AdmissionClass)),
                "artifact_read": {
                    "format": _RANGE_FORMAT, "max_chunk_bytes": ARTIFACT_READ_MAX_BYTES,
                    "default_channel": "legacy", "channels": ["legacy", "structured", "resource"],
                },
                "deferred_consequences": True,
                "native_human": "NOT_RUN",
                "live_ac10": "NOT_RUN",
                "both_modes": "NOT_RUN",
                **status,
                "live_status": "PREREQUISITES_PRESENT" if status["provider_ready"] else "BLOCKED",
                "production_ready": False,
                "admission_mode": active.admission_mode,
                "human_grant_channel": (None if active.admission_mode == "task_scope"
                                        else "standard_mcp_form"),
                "form_candidate_unverified": active.admission_mode == "standard_form",
            }
        )

    @server.tool()
    async def present_admission(
        run_id: SafeId,
        proposal_id: SafeId,
        digest: Digest,
        ctx: Context[Any, Any],
    ) -> MCPReply:
        """Offer one admission. Strict args. Standard MCP form can record; it is not native-human proof."""
        token = _mcp_ctx.set(ctx)
        try:
            result = await active.present_admission(run_id, proposal_id, digest)
            return _reply(_result(active, result=result))
        except Exception as exception:  # noqa: BLE001 - sanitized tool boundary
            return _error(exception)
        finally:
            _mcp_ctx.reset(token)

    close_tool_arguments(server)
    return server

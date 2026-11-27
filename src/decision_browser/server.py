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
    ToolAnnotations,
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
from decision_browser.mcp_outputs import ArtifactBody, CapabilityFacts, TaskSnapshot
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
TaskReply = Annotated[CallToolResult, TaskSnapshot]
ArtifactReply = Annotated[CallToolResult, ArtifactBody]
CapabilitiesReply = Annotated[CallToolResult, CapabilityFacts]
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

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
    ))
    async def start_task(
        request: Annotated[TaskRequest, Field(description="Bounded browser task: goal/start URL, input values, independent checks, budgets and privacy policy. No caller-supplied approval or grant fields.")],
    ) -> TaskReply:
        """Start one asynchronous browser task that can interact with external pages.

        Call once for a new task, after checking capabilities and agreeing the scope
        with the user. This launches real work; it is not a preview or an approval.
        Returns result (including run_id) and pending_proposal, initially often null
        with reason preparing_admission. Poll get_task; when awaiting_human_admission
        with a pending proposal, use present_admission with its exact identifiers.
        If the start response is lost, call get_task without run_id to recover the
        single retained run. Never retry start_task automatically: a retry can start
        new work after retention expires. busy means a run is still retained.
        Tool errors use isError=true and {"error": code}; inspect them before acting.
        """
        return _guard(lambda: _result(active, active.start_task(request)))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_task(
        run_id: Annotated[SafeId | None, Field(description="Exact result.run_id from a task response. Omit or pass JSON null to recover the single retained run after a lost start response; the string 'null' is an ordinary ID.")] = None,
    ) -> TaskReply:
        """Read the current snapshot of one retained task without starting work.

        Supply run_id to poll that run; omit it (or pass JSON null) to recover the
        single retained run after a lost start_task response. Returns result and
        pending_proposal. queued, awaiting_human_admission and running are not final;
        reason finalizing means cleanup is still in progress. Present a pending
        admission only with its exact identifiers; read result.artifact_ids with
        read_artifact. A terminal outcome_unknown/cleanup_unknown is not proof of
        successful cleanup. unknown/expired errors mean no matching retained run,
        not evidence that no browser action occurred. Do not restart automatically.
        """
        return _guard(lambda: _result(active, run_id))

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
    ))
    async def cancel_task(
        run_id: Annotated[SafeId, Field(description="Exact result.run_id of the retained run to stop; obtain it with get_task if the start response was lost.")],
    ) -> TaskReply:
        """Request cancellation of a retained browser run and revoke its admission.

        Use when the user wants the run stopped. Returns result and pending_proposal;
        cancellation is a request, not an immediate cleanup receipt or rollback of
        earlier page changes. Poll get_task until finalization/cleanup finishes.
        outcome_unknown with cleanup_unknown requires inspection, not a success claim
        or automatic restart. Repeated cancellation of the same retained run adds no
        new work; unknown/expired returns isError=true with {"error": code}.
        """
        return _guard(lambda: _result(active, result=active.cancel_task(run_id)))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def read_artifact(
        run_id: Annotated[SafeId, Field(description="Exact result.run_id owning the retained artifact.")],
        artifact_id: Annotated[SafeId, Field(description="One ID from result.artifact_ids, not a filesystem path or URL.")],
        offset: Annotated[Offset, Field(description="Zero-based byte offset in the original artifact, not a base64 character offset. For pagination, copy next_offset from the prior response.")] = 0,
        limit: Annotated[ReadLimit, Field(description="Maximum original bytes per chunk (1..65536), before base64 encoding.")] = ARTIFACT_READ_MAX_BYTES,
        channel: Annotated[Literal["legacy", "structured", "resource"], Field(description="legacy: small whole artifact in text and structuredContent; structured: chunk base64 only in structuredContent; resource: chunk base64 in an embedded resource, metadata in structuredContent.")] = "legacy",
    ) -> ArtifactReply:
        """Read retained evidence bytes by server-issued run and artifact IDs.

        Use result.artifact_ids from get_task, never a local path. Default legacy is
        only for a whole artifact fitting one chunk at offset 0; otherwise it returns
        artifact_range_required. Choose structured or resource for large artifacts.
        Follow next_offset with the same IDs/channel until eof=true; offset and limit
        count original bytes, not base64 characters. Explicit channels include total_bytes,
        sha256 (whole artifact) and chunk_sha256. structured returns base64 in
        structuredContent.content; resource returns it in content's embedded blob.
        Errors retain isError=true and {"error": code}, including bad_range for an
        offset beyond EOF; unknown/expired runs cannot supply retained artifacts.
        """

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

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def capabilities() -> CapabilitiesReply:
        """Inspect local prerequisites and supported operations before starting a task.

        Takes no arguments. Returns browser modes, operations, admission classes/mode,
        artifact chunk limits/channels and selected-provider configuration status.
        provider_ready and PREREQUISITES_PRESENT describe configuration only: no API,
        browser, entitlement or human approval is tested. production_ready remains
        false and live/native evidence remains NOT_RUN. A configured provider does
        not guarantee successful execution; resolve BLOCKED prerequisites before use.
        No credentials are returned and this call does not grant admission.
        """
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

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
    ))
    async def present_admission(
        run_id: Annotated[SafeId, Field(description="Copy pending_proposal.run_id exactly from the latest task snapshot.")],
        proposal_id: Annotated[SafeId, Field(description="Copy pending_proposal.proposal_id exactly; do not reuse an old or resolved proposal.")],
        digest: Annotated[Digest, Field(description="Copy pending_proposal.digest verbatim: 64 lowercase hexadecimal characters binding the displayed scope. Never calculate or guess it.")],
        ctx: Context[Any, Any],
    ) -> TaskReply:
        """Present a pending action scope through the client's Standard MCP form.

        Call only when get_task reports awaiting_human_admission and a non-null
        pending_proposal. Copy run_id, proposal_id and digest from that same proposal.
        The tool presents a form; calling it does not itself grant permission. Only
        the supported form response may record admission, and a host accept is not
        proof of a native human click. Never fabricate reviewed/approval/grant fields.
        Returns the updated result and pending_proposal; continue with get_task.
        Unsupported form clients fail closed. Stale, resolved or concurrent requests
        may return isError=true with unknown, presentation_resolved or presentation_busy;
        refresh get_task instead of blindly repeating a prompt.
        """
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

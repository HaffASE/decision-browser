# Native host acceptance procedure

Native host end-to-end, current App UI, human acceptance/decline/cancel, and
direct/implicit skill execution remain **`NOT_RUN`** for current acceptance.
Earlier transport or schema observations are partial historical evidence,
not a current native pass. Python SDK, scripted host, unit, and owned-local
browser tests do not satisfy these gates. Use the
[verification profiles](../../docs/verification.md) for those local tests.

## Current admission contract

The default MCP server uses `StandardFormDesk` and its runner's `AdmissionStore`.
Explicit host action `accept` with strict `reviewed: true` can create one exact
live production record after same-pending-object, exact binding, production
state, freshness, deadline, and revocation checks. This is sufficient ordinary
protocol admission and is not evidence of a native human click. A scripted host
response can exercise this record path in tests; it cannot establish human
acceptance. A bare boolean without a matching live record is insufficient.

The form schema is an object with one required boolean property, `reviewed`.
The raw request carries the related MCP request id, omits root `title`, and
sets `additionalProperties: false`. The pinned MCP SDK preserves that schema
on the wire. Verify acceptance of this exact schema in each supported client.
Returned content is strictly validated with extra fields forbidden. The model
and skill must never auto-accept or fill `reviewed: true`. No bespoke click broker
is required for the ordinary standard-form channel. `ProductionDesk` outside MCP remains a
separate fail-closed desk (`no_supported_human_grant_channel`).

## Authorized native procedure

Use a standard-form-capable host and the opt-in
[project-local registration](../../examples/codex.project.toml), adapting paths
to the chosen environment. Verify the effective connection without changing
global settings. Real-provider use and browser scope require separate explicit
authorization. The [bounded counter procedure](../../docs/browser-control-stage-one.md)
is the first production-path control scenario; merely registering or starting
its server does not run it or approve admission.

Complete this procedure separately in Codex and Claude Code. Record the client
version, operating system, candidate commit, installed artifact SHA-256, provider
and model, and each outcome below. Keep the provider key out of the record.

| Client | Installation and connection | Accept, decline, form cancel | Bounded task and cleanup | Explicit task cancellation | Direct and implicit skill execution |
| --- | --- | --- | --- | --- | --- |
| Codex | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN |
| Claude Code | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN |

Replace a cell only with a result linked to evidence for the reviewed candidate.
Record the counter task's independent `#count = 3` check and final cleanup
outcome with its request and result. A failed or blocked run stays visible in
the record when a later run succeeds.

1. Discover the exact six tools. Call `capabilities` with `{}`. The supported
   operations are scroll, wait, screenshot, navigate, click, fill, select,
   enter, and submit. There is no exposed inspect/JavaScript dispatch tool.
   Key presence and `production_ready: false` are prerequisite facts.
2. Exercise direct `$decision-browser` and implicit browser-task invocation
   separately. Supply only the closed request fields: goal, start URL, mode,
   values, checks, viewports, limits, privacy, and visual. Record actual skill
   execution; discovery alone does not satisfy either path. A pure API question
   must start no browser task.
3. Start each logical request once. Poll `get_task` for its pending proposal,
   then call `present_admission` with the exact run id, proposal id, and digest.
   If the start reply is lost, recover through `get_task` without an id;
   do not resend the start. A wrong digest cannot create a grant.
4. In separate fresh, authorized runs, have the human review the displayed
   immutable scope and answer the form. Record accept, decline, and cancel
   independently. Decline or `reviewed: false` cancels the proposal; form cancel
   revokes the run. Malformed content, expiry, changed identity, revocation,
   duplicate acceptance, and replay must not create a new live record.
   Never substitute a model answer for the human interaction.
5. Poll through `running` / `finalizing` until a public terminal result is
   available after diagnostics and owned cleanup. Functional, visual,
   development-only, and overall terminal status are separate evidence fields.
6. Read artifacts with the server run/artifact IDs within retention, decode
   base64, and verify each SHA-256. Preserve the request, exact commit, host
   version, protocol observations, final result, and bounded evidence without
   credentials or unrelated browsing data.
7. Test explicit cancellation separately. Confirm cleanup of owned resources
   and no replay of an uncertain dispatch. Restart removes in-memory records;
   an old id or replay is not a new admission and does not prove no prior effect.

Keep native host, human interaction, real provider, external AC-10, existing
Chrome, and visual acceptance results distinct. The
[live procedure](../live/README.md) defines those additional boundaries.

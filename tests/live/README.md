# Live acceptance scope

Real-provider acceptance, external AC-10, native end-to-end, human interaction,
existing Chrome ownership/cookie isolation, and both-mode completion remain
**`NOT_RUN`** unless supported by a separate fresh acceptance record. Scripted
providers, local browser fixtures, protocol acceptance, and a passing CI job do
not establish them. The counter collector explicitly keeps provider/human
claims **`UNVERIFIED`**, even if its structural checks pass.

## Before a live run

- Obtain the concrete authorized site/task, inputs, browser mode/profile,
  action and spending bounds, privacy scope, and required approvals. An example
  URL or test procedure is not authorization. Known purchase, send, publish,
  delete, transfer, and booking consequences remain blocked.
- Configure the production credential securely outside the test evidence.
  The default OpenAI route reads `OPENAI_API_KEY`; other routes use their own
  selected credentials or a separately configured local service. See the
  [provider guide](../../docs/providers.md). Key presence does not prove
  entitlement, outbound HTTP, or a successful model response.
- Use the normal `StandardFormDesk`. Human host action `accept` with strict
  `reviewed: true` can create the exact live production record after identity,
  binding, freshness, deadline, and revocation checks. Protocol acceptance is
  not human-origin proof; preserve actual human host interaction separately.
  Never have a model accept or populate the form. `ProductionDesk` outside MCP
  still fails closed; no bespoke manual broker is required for the MCP path.

Start with the [bounded counter scenario](../../docs/browser-control-stage-one.md),
using its normal production provider and admission path. Separate structural
checks from evidence of actual Decisions requests/responses and human
interaction. A provider constructor or method call count alone proves neither
network destination nor a live successful response. Keep exact commit,
request scope, results, budgets, paired action journal, artifact hashes, and
cleanup status; do not record keys or private cookie values.

## Separate browser-mode acceptance

Isolated mode creates a fresh browser context. Existing Chrome creates an owned
tab in the selected browser's existing context; it does **not** create a private
context and can share that context's cookie state. Attachment alone does not
establish signed-in provenance or consent to use a profile.

With explicit authorization, first use a controlled profile and synthetic
owned-origin sentinel cookie. Inventory pre-existing targets; verify exactly
the owned target is added and removed, the Chrome process and pre-existing
targets remain, and sentinel state is preserved. Record sentinel visibility
in existing mode and absence in a fresh isolated run separately. If private
context isolation is required, existing-context behavior is a blocker. A mock
driver, protocol attachment, or isolated-mode pass cannot close this gate.

## What to report

For each attempted flow, retain final functional checks, refusals, limits,
unexpected/background effects, uncertain outcomes, and cleanup. Read-only
intent does not establish the absence of background effects. Never replay an
unknown action or convert missing evidence into a pass.

External AC-10 requires its own concrete authorized flows; no owned-loopback
result establishes general-site reliability. Visual capture, image privacy,
model visual assessment, seeded defects, and responsive-layout QA are a later
separate deliverable. Keep functional and visual status separate.

See [native acceptance](../native/README.md) and
[local/CI verification](../../docs/verification.md) for their narrower claims.

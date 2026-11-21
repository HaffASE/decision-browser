---
name: decision-browser
description: Run finite browser work in isolated Chromium or an existing Chrome via the decision-browser stdio MCP. Use for ordinary navigation and forms and for functional or visual QA with click, fill, native select, enter, submit, scroll, wait, or screenshot. Skip pure API questions and translations. Known real purchase, send, publish, delete, transfer, or booking consequences stay blocked in v1.
---

# Decision browser

Thin workflow for ordinary browser tasks and for functional and visual QA. These instructions describe a packaged MCP skill. A local checkout is not a live server and not proof the tools are installed or passed natively.

## When to run

Use for active browser work on an isolated Chromium or an existing Chrome whose origin a human already scoped. That includes ordinary navigation and forms, plus functional and visual QA.

Do not start a browser for pure API questions, translations, or other non-UI work. Known real purchase, send, publish, delete, transfer, or booking consequences are blocked in v1. They are not ordinary permission.

## Prerequisites

Read schemas from the connected tools. Do not invent request fields. Expected tools: `capabilities`, `start_task`, `get_task`, `cancel_task`, `read_artifact`, `present_admission`.

If that connection is absent, explain the missing prerequisite. Do not claim a browser ran.

The client resolves the directory that contains the loaded `SKILL.md` to an absolute `SKILL_DIR` before it runs the doctor, then executes the doctor with the Python interpreter in the installed MCP registration: `"$MCP_PYTHON" "$SKILL_DIR/scripts/doctor.py"`. For a development checkout, use its prepared environment. An unrelated system Python may report missing dependencies. The doctor reports Python, platform, pinned package compatibility, server importability, selected-provider prerequisites, and whether an optional explicit project path exists. `api_key_present` is a boolean for the selected provider only. `local_ready` and `live_ready` reflect prerequisites only (`readiness_scope` is `prerequisites_only`). Neither boolean is live, native, browser, entitlement, or human evidence. It does not call APIs, use a browser, or print secret values or exception details.

`capabilities` may report `production_ready: false`, `native_human: NOT_RUN`, `live_ac10: NOT_RUN`, `both_modes: NOT_RUN`, `human_grant_channel: standard_mcp_form`, and `form_candidate_unverified: true`. Those fields are not production, native, live, or human proof. `provider_ready` and `live_status` describe configuration prerequisites; a keyless local provider can be ready even when its server is not running. Do not print credentials.

The server operator selects OpenAI (default), TypeSafe Jev, OpenRouter, OpenCode Console, Gemini Decision 1 through OpenCode (`gemini`), or a loopback System One endpoint through environment configuration. Do not add provider, endpoint, model or key fields to tool arguments. Check `text_generation` and `visual`: alternative decision providers need a separately configured text helper for unbound field values and currently report visual inspection as unsupported. Supplied task values can still fill fields without a helper. An incomplete visual diagnostic is separate from functional checks; do not silently switch providers to complete it.

## Actions

Code owns targets, classification, and execution. The model does not author JavaScript or freeform selector actions, and it does not pick each DOM step.

Allowed code actions: navigate, DOM click, fill, native select, enter, submit, scroll, wait, and screenshots. Unsupported: iframe, shadow DOM, canvas, file upload, download, and new tab. Under `standard_form`, an unsupported mechanism ends the run with a reason. Under `task_scope`, unsupported candidates can be excluded while supported work continues; if no supported choice remains, the run ends with a reason. Neither mode falls back to the OS, arbitrary eval, or another tab.

Modes are `isolated` and `existing_chrome`. Keep functional status separate from visual findings.

## Start once

Call `start_task` once per logical request. It returns fast with a server `run_id` while admission is still pending, and dispatch stays 0. A separate explicit new logical request is another start. A second concurrent start is busy. Exactly-once delivery is not guaranteed.

If that reply is lost, use bounded `get_task` (optional `run_id`, or no id for the single held slot). Never send a second start automatically. `notFound`, an empty reply, or a restart is not proof the first start was a no-op, and it does not authorize a repeat, a new start of the original task, or moving the record.

`get_task` is a fast read-only lookup.

## Admission

Read `capabilities.admission_mode`. With backend policy `task_scope`, ordinary
tasks in isolated mode are admitted for their exact initial origin, operations,
budgets, and deadline without a form. Start once and poll normally. This is
`ordinary_task_scoped`, not a standard-form record or native-human proof. Do not
call `present_admission` without a pending proposal or synthesize a host response.
Consequential actions remain blocked. A foreign or uncertain action scope ends
with a reason; this mode never silently expands scope or auto-accepts beta.

The remaining form instructions apply to `standard_form` (the default) and to
older connected servers that do not report an admission mode.

Call `present_admission` with the exact server `run_id`, `proposal_id`, and `digest`. The host presents the request-scoped standard MCP form. This skill never accepts that form, never synthesizes `reviewed: true`, and never fills grant, receipt, or source flags.

On the MCP server, `StandardFormDesk` can write one exact live production record in the same `AdmissionStore` as the run manager. The write runs only after host action `accept` with strict `reviewed: true`, and only while that same proposal is still pending, non-development, fresh, and unrevoked. Identity, digest, state, freshness, deadline, and revocation are rechecked. That record is protocol admission under the standard manual host channel. It is sufficient for ordinary admission. It is not native human-click proof. `native_human` stays `NOT_RUN`. Bespoke host-origin or source attestation is not required.

Decline and `reviewed: false` cancel that proposal. Form cancel revokes the run. Expiry, restart, and replay fail closed. The model cannot mint a grant from an argument, file, receipt, environment variable, or `auto_accept`.

`ProductionDesk` outside MCP still fails closed (`no_supported_human_grant_channel`) and creates no grant. Keep that desk separate from in-MCP `StandardFormDesk`. With no live matching record, mutation dispatch outside `fixture_mapped` and `observe_driver` stays 0. Consequential classes stay blocked.

Scope is the exact human origin, whether that origin already existed or is new. Expansion needs a new human proposal and digest. There is no global registry.

## Budgets and cancel

Keep the original finite budgets and deadline: steps, time, calls, and byte limits. Human wait stays inside that deadline. Bounded cleanup grace adds no task actions, provider calls, or extension. `cancel_task` only for resources this run owns.

## Evidence

Keep functional verification separate from vision findings. Empty findings are not proof the page is clean. A missing key, a refusal, incomplete capture, or a broken diagnostic is not PASS. Visual status does not set functional `verified`. A fake provider is an explicit development-only result and is never a live or native pass.

Read artifacts with `read_artifact` by server IDs, not filesystem paths. Do not send secrets, cookies, or full sensitive URLs. An unsafe capture means zero affected transmissions.

## Examples

- Ordinary navigation or a form, and a seeded visual-defect check, each start once. The server runner decides browser actions. Codex polls `get_task` and reads `read_artifact`. Call `present_admission` with the server run, proposal, and digest and let the host show the manual form. Do not accept it or set `reviewed`. Inside MCP, host accept with strict `reviewed: true` on a still-pending fresh proposal can create the live store record. That record is not a native click. `ProductionDesk` outside MCP still grants nothing.
- Existing Chrome stays inside the human's exact origin. A new origin needs a new proposal and digest.
- A pure API question, a translation, or a real purchase, send, publish, delete, transfer, or booking must not start a browser as an ordinary task.

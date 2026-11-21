# Stage one: browser control through Decisions

The first deliverable is a working Jev-ultrafast-style browser control loop:

`DOM observation -> decision(operation, compatible target) -> guarded action -> new DOM observation`

Visual QA is a later, separate deliverable. Stage one uses `visual: false`
and `privacy.send_images: false`. Geometry and freshness guards still protect
ordinary DOM actions; they are execution checks, not model visual assessment.

This stage uses the existing runner, provider, MCP tools, and admission path.
It does not replace the aggregate G009 objective or claim that its other
acceptance requirements have passed. The original accepted plans and their
evidence remain preserved.

## Reference and existing implementation

- [Jev-ultrafast](https://github.com/browser-use/jev-ultrafast): structured DOM
  observation, operation/target heads, guarded execution, and stale recovery.
  The inspected reference is commit
  `1231850a0bf1a0c0341fe408ef1668dbbfdfac46`; this is not a claim about latest HEAD.
- [OpenAI Decisions](https://developers.openai.com/api/docs/guides/decisions):
  the application supplies the choices; the model selects among them.
- `src/decision_browser/runner.py`: `_questions`, `_choose`, `_dispatch_one`,
  and `_loop` implement the control loop. `_visual` returns when visual is false.
- `src/decision_browser/provider.py`: `decide` uses the Decisions endpoint.
- `src/decision_browser/dom.py`: observation, target preparation, freshness
  guards, and native DOM action dispatch.

Explicit `values` avoid the separate text-generation helper for known field
contents. Functional `checks` validate the resulting DOM independently of
the model's `DONE` answer. An empty check list produces an unverified result.

## Reproducible first scenario

The bounded executable serves the same counter as the test fixture from an
owned ephemeral IPv4 loopback listener. It uses `create_server`, the normal
`AdmissionStore` and `StandardFormDesk`, and `production_factory`. It does not
start a task or answer admission forms. No development grant is installed.

Install the locked project first (see [verification](verification.md)), then
configure a standard-form-capable MCP host to launch this command:

```sh
/path/to/.venv/bin/decision-browser-counter --output-dir /absolute/new/private/counter-evidence
```

The equivalent source-checkout wrapper is:

```sh
.venv/bin/python scripts/run_counter_acceptance.py --output-dir /absolute/new/private/counter-evidence
```

The output directory must not already exist. Diagnostic messages use stderr;
stdout remains exclusively MCP protocol traffic. No credential is collected by
this command. A separately authorized live test needs the production provider
credential already configured securely, an installed browser, and a human at
the host. Starting this server alone launches neither browser nor provider.
Do not launch or approve a live test without authorization for those actions.

1. Read the generated `request.json` in the output directory. Its random port
   is valid only for this server instance. Start promptly: the listener expires
   after 120 seconds if no task starts. Once started, its expiry covers the
   task's original 120-second budget plus two seconds of cleanup grace; idle
   setup time cannot shorten that budget.
2. Submit that exact request to `start_task` once; retain the returned run id.
   Altered requests and a second start are rejected. There is no auto-retry.
3. Poll `get_task` for the pending proposal. Pass its exact run id, proposal id,
   and digest to `present_admission`. The human reviews and answers the host's
   standard form. Neither the model nor this collector answers it.
4. The retained collector polls the public final result, including cleanup,
   exports `result.json`, raw `artifacts/<id>`, their `artifacts.json` manifest,
   and `report.json`, then closes the fixture. Polling need not stay active in
   the host for collection to finish. Evidence is copied promptly within the
   runner's 60-second final retention window.
5. Disconnect the MCP session after collection. Disconnect, denial, deadline,
   no-start shutdown, and evidence-write failure also close the owned fixture.
   Review `report.json`; a protocol disconnect is not an acceptance result.

The generated request fixes isolated mode, three increment clicks, a `#count`
text check for `3`, no visual QA, and no model image transmission. Limits are
four actions including bootstrap navigation, four provider calls, 120 seconds
including admission, 64 KiB input, 64 KiB output, and 1 MiB stored artifacts.
A Decisions-only wrapper rejects reset, screenshot, fill, select, navigation,
and unknown targets before preparation or dispatch. The runner's four-step
limit permits bootstrap plus at most three dispatched increments. A stale
choice consumes a provider call, not an increment allowance, so one retry
fits within the four-call cap. The wrapper never substitutes a model choice.
The Responses/text helper is disabled.

## Acceptance of this scenario

`report.json` and its exported evidence are local diagnostic files. JSON exports
use atomic replacement to avoid partial reads during ordinary operation; they
are not a crash-durable audit store and do not promise persistence after power
loss. Keep an external verified archive if durable retention is required.

`report.json` separates structural technical checks from live acceptance:

- `structural_status: PASS` requires a final functionally verified,
  non-development result, exactly one bootstrap navigation plus three `inc`
  clicks, acknowledged paired journal outcomes for the same run, valid
  artifact hashes, budget compliance, and the real strict functional-report
  schema: a failed `#count` text check after bootstrap and each of the first
  two clicks, followed by a verified check after the third click. Expected
  text `3` comes from the frozen canonical request; reports never contain raw
  page text or an invented `expected` field. Arbitrary JSON side records and
  extra journal fields fail validation.
- Each click's admission binding must match the immutable alpha proposal
  observed by the manager after admission. Its recorded scope, canonical URL,
  budgets, run id, and recomputed proposal digest are checked. This is historical
  scope evidence, not new admission authority: cleanup revokes the live record.
  An alpha scope digest intentionally differs from each per-action digest.
- An independent close-time read obtains the exact fixture URL and `#count`
  value atomically before browser teardown, within the existing 0.5-second
  read cap. Exact `3` is required; whitespace, a foreign URL, read failure or
  cancellation cannot pass. Browser and fixture cleanup must also succeed.
  The runner currently leaves optional `last_url` unset, so the collector
  preserves that raw result and uses the independent URL read; a conflicting
  non-null `last_url` is rejected.
- Wrong counts, reset/screenshot evidence, missing or orphaned outcomes,
  altered hashes, non-final results, and uncertain cleanup fail that gate.
- `live_provider: UNVERIFIED`: construction, Decisions method invocations,
  and returned decisions are recorded separately. They do not prove actual
  outbound HTTP, its destination/model, or a live successful provider response.
- `human_admission: UNVERIFIED`, `native_host: NOT_RUN`, and
  `live_acceptance: UNVERIFIED` remain explicit. A standard-form host accept
  is not proof of a human click. No supplied boolean upgrades these claims.

A separately reviewed live acceptance record must establish actual Decisions
calls and human host interaction. This collector cannot auto-PASS that record.
The process exits zero only for an exported structural PASS. FAIL, ERROR,
NOT_RUN, and report-write failure exit nonzero. A zero exit code still does not
establish live or human acceptance.

Offline doubles, even with passing structural checks, never establish live
acceptance. The unit suite does not launch a browser, invoke a live API, or
collect credentials. General-site reliability and existing Chrome remain
separate acceptance tasks.

## Later blocks

After the first scenario passes, verify fill, native select, navigation,
scroll/wait, and stale recovery with real decisions. Verify the selected
existing Chrome mode separately. General-site reliability is not established
by the counter scenario.

### Runner control regression gates

`tests/integration/test_runner_controls.py` supplies six **scripted/development**
runner chains on an owned ephemeral IPv4 loopback listener. The provider chooses
from the runner's semantic descriptors, not the first target or a hard-coded DOM
token. The ordinary runner, native DOM dispatch, policy, journal, and functional
checks remain in the chain. The explicit development fixture mapping covers
routed actions; wait retains the normal `observe_driver` policy. No production
admission behavior is changed.

The gates below describe assertions, not a claim that a native run passed:

| Operation | Required evidence from its runner chain |
| --- | --- |
| Fill | Select the non-first field by name; use nonempty Unicode from `TaskRequest.values`; forbid the text helper; exact final value and one input event; decoy field unchanged with zero events. |
| Select | Choose the non-first menu and non-first option by its label, which differs from its value; dispatch the underlying value; exactly one change event; decoy menu unchanged. |
| Navigate | Choose between two identically captioned links using the descriptor destination; exact final URL and route content; one bootstrap plus one navigation; no hit on the wrong route. |
| Scroll | Dispatch `down`; independently read positive scroll position; no wait or field change. Empty functional checks intentionally leave this result unverified at its two-action budget, rather than manufacturing a verified result. |
| Wait | Dispatch `100` milliseconds exactly once; arm an owned-page state transition at the native wait boundary, after provider selection; separately await its readiness signal within a two-second bound, then observe the state and unchanged scroll position. This proves the binding and observable state, not elapsed time; no model-latency or wall-clock duration assertion. |
| Stale recovery | Mutate the first target during `decide`; stale guard, zero old effects and no old native dispatch; reobserve and select its fresh semantic descriptor; one acknowledged fresh click and no replay. |

Fill, select, navigation, wait, and stale recovery require independent declared
functional checks and paired before-dispatch/acknowledged journal evidence, in
addition to native-call and close-time DOM/event observations. Every run is
explicitly `development_only: true`, `effect_guarantee: none`, `visual: false`,
and `privacy.send_images: false`. These checks do not invoke a live provider.

`tests/unit/test_runner_control_bindings.py` runs without Chromium: exact field,
option, destination, direction, and 100 ms bindings, plus the real runner's stale
retry/policy/journal sequence with explicit DOM doubles. This is browser-free
regression evidence, not evidence of native browser effects.

Run the locked [verification profiles](verification.md) at a clean commit. The
offline profile covers these units. The full profile checks the browser
prerequisite before running the full suite. To inspect the six integration cases
without launching Chromium, use the selected environment's Python:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /path/to/venv/bin/python -I -m pytest \
  -p pytest_asyncio.plugin --collect-only -q tests/integration/test_runner_controls.py
```

Collection alone is **NOT_RUN** for native effects. In the current implementation
environment, Chromium is unavailable; the new native runner gates remain
**NOT_RUN**, pending a sandbox-capable browser environment and a full-profile
execution. Do not convert a missing browser into a skip/pass or disable its
sandbox to obtain a pass. Preserve the exact commit, profile, source manifest,
logs, JUnit counts, and blocker in each evidence record.

### Separate acceptance still NOT_RUN

- **Live Decisions/native host/human admission: NOT_RUN for these controls.**
  After separately authorizing the provider use and browser scope, exercise the
  same bounded checks through the normal production admission path. Retain
  actual provider and human-interaction evidence separately from scripted tests.
- **External AC-10: NOT_RUN.** Before testing any external site, obtain the
  concrete authorized site, task, inputs, action/spending bounds, privacy rules,
  and relevant approval. A planning itinerary, example URL, or test procedure
  is not authorization. Do not bake personal travel facts into fixtures. Record
  individual attempted flows, final checks, refusals, limits, unexpected effects,
  and cleanup; an owned-loopback result establishes no general-site reliability.
- **Existing Chrome ownership/cookie isolation: NOT_RUN.** Authorize the selected
  `existing_chrome` profile and attachment explicitly before any test. First use
  a controlled browser/profile with only synthetic data: inventory existing
  targets, seed an owned-origin sentinel cookie, and verify the runner creates
  only its owned target. The current existing-mode driver uses the selected
  browser's existing context; an owned tab does **not** imply cookie isolation
  from that context. Record sentinel visibility in existing mode separately
  from its absence in a fresh isolated run; if private-context isolation is
  required, treat the existing-context behavior as a blocker. Confirm cleanup
  removes only owned resources, preserving pre-existing targets, sentinel state,
  and the Chrome process. Record observations rather than real cookie contents.
  A mocked driver, isolated-mode pass, or existing-mode protocol connection
  alone does not satisfy this gate or establish signed-in provenance.

Visual capture, image privacy, model visual assessment, seeded visual defects,
and responsive-layout QA belong to the later visual deliverable. They are not
prerequisites for acceptance of the browser-control scenario above.

## Current limitations

`visual: false` alone does not remove screenshot from the general runner
action set. The dedicated counter executable rejects any screenshot decision
before dispatch. This narrow guard does not change the general runner profile.

Admission, original deadlines, refusal handling, unknown-outcome no-replay,
and owned-resource cleanup remain requirements of browser control. Separating
visual QA does not remove them. Stage evidence is saved separately from the
previous G009 checkpoints.

## Historical evidence from 2026-10-09

The existing runner and freshness suites plus three targeted provider tests
passed: **21 tests in 20.17 seconds**. The example passed `TaskRequest`
validation and its page matches the existing fixture literal exactly.
All 83 paths pinned by the previous source checkpoint retained their hashes.

One real connected MCP task was started, with no development provider or
fixture grant. Its admission form expired at the original 120-second deadline:
`limit_exceeded`, zero provider calls, zero browser actions, functional status
`not_run`, visual diagnostic `not_requested`. The temporary HTTP server exited.
The live control loop has therefore not passed this scenario. The task was
not restarted and the model did not accept the admission form.

These earlier observations are not a fresh verification of the current tree.
Use the [verification profiles](verification.md) and a separately authorized
native acceptance record to establish new results.

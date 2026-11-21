# Owned browser scenario corpus

The corpus extends the five local comparison regressions with ten multi-step
workflows. Every page and value is synthetic and served on a fresh loopback
origin. The production runner uses `task_scope` admission and an isolated,
sandboxed Chromium. No existing user browser or public website is opened.

CI supplies semantic scripted decisions. This exercises actual DOM collection,
action guards, input, navigation, verification and cleanup, but does not measure
a model's ability to choose actions. The same fixtures can exercise the
configured production provider through the explicit development command below.
For scripted runs, delayed menu/form reveals are held until the policy observes
the missing control and chooses WAIT. This fixture-only handshake makes waiting
coverage deterministic even on a slow host. Configured providers use the normal
500-ms page delay, with no reveal hook or scripted plan supplied to the model.

## Workflows

| ID | Goal | Required outcome |
| --- | --- | --- |
| `hotel` | Search Lisbon, select Design and Free cancellation, open Casa Flora | Exact property route and all applied filters; one event for each effect |
| `profile_form` | Enter Ada Lovelace and ada@example.com, select Express, confirm the local profile | Exact values on confirmation and exactly one complete submission |
| `autocomplete` | Choose Zurich Airport (ZRH), with city and airport-hotel distractors | Exact field value and airport event; suggestions close |
| `calendar` | Choose September 20, 2026, with another month and day offered | Exact ISO date in the field, confirmation and event |
| `styled_filter` | Enable the Korean-labelled brand through its overlaid label | Checkbox checked, filtered state and one enabled event |
| `consent` | Reject optional cookies, then search from London | Banner gone, exact origin and rejection recorded before search |
| `delayed_menu` | Reveal a delayed news menu and open Society | Exact destination URL, Society headlines and one route event |
| `loading_overlay` | Wait for the service form, then search for climate | Search only after the loading layer is gone; exact submitted query |
| `below_fold` | Scroll to Evidence and mark it read | Actual viewport scroll and reached section, plus one action event |
| `article` | Search and open the incompleteness-theorems article rather than Gödel's biography | Exact article route and search/visit events |

A passing workflow requires all three: `terminal=verified`,
`functional=verified`, and the independent server oracle. The oracle compares
the complete event sequence and payloads, including duplicate effects. A DONE
decision is tested against every untouched workflow and must fail. Wrong airport,
month, article, section and wrong-brand filter cases must also fail. Separate
oracle checks reject missing submission fields and repeated submission events.

The profile form is a synthetic data-entry task. It does not place an order or
relax the production policy for purchases. The article is a controlled local
analogue of the Wikipedia example, not an external-site acceptance result.

## Live-provider results: 17 November 2026

Each workflow ran three times with real provider calls and an isolated Chromium.
The table counts confirmed attempts, including native completion, independent
page and event checks, cleanup, and complete reported input/output usage. Terra
uses the standalone Playwright agent; Core uses its production provider adapters
and `task_scope` development admission. The goals, supplied values, checks and
case budgets are shared across all three arms.

| Workflow | Core · TypeSafe Jev 1.14.0 | Core · OpenAI Decisions | Terra 6.1 · medium |
| --- | ---: | ---: | ---: |
| `hotel` | 0/3 | 2/3 | 3/3 |
| `profile_form` | 1/3 | 2/3 | 3/3 |
| `autocomplete` | 0/3 | 3/3 | 3/3 |
| `calendar` | 0/3 | 3/3 | 3/3 |
| `styled_filter` | 1/3 | 3/3 | 3/3 |
| `consent` | 1/3 | 3/3 | 3/3 |
| `delayed_menu` | 0/3 | 2/3 | 3/3 |
| `loading_overlay` | 2/3 | 3/3 | 3/3 |
| `below_fold` | 0/3 | 3/3 | 3/3 |
| `article` | 0/3 | 3/3 | 3/3 |

These ten workflows are part of the complete 21-case matrix. The
[dated report](benchmarks/2026-11-17-browser-matrix/report.md) includes every
attempt, errors, per-call and per-case tokens, the single HTTP 503 with missing
usage, and the audit of the reporting correction. OpenCode Go was skipped.

The [failure rerun](benchmarks/2026-11-17-browser-remediation/report.md) reports
the same cases after the fixes, with their original repetition IDs and
separate confirmation counts, timings, token usage, and remaining errors.

## Race and stopping cases

The ordinary decision context includes bounded progress hints: a fill choice
states whether its current value matches the uniquely bound caller value,
completion feedback counts pending independent checks, and scroll feedback
states whether more content exists above or below. Raw supplied values and
check targets/expectations are not added by these hints. Editable comboboxes can
be filled directly; readonly picker triggers still require a click and a new
observation. These hints do not remove choices, retry DONE or alter verification.

Checked tasks in isolated `task_scope` mode also have a narrow recovery for a
repeated focus click. After an acknowledged click, the runner can omit that
field's click choice for the next observation only when a uniquely bound value
already matches, focus was observed on the field before and after, document,
URL, semantic state and CDP node identity still match, and pending checks did
not advance. Other operations on the field remain available. A fresh observation
restores click eligibility. Picker hints, comboboxes, readonly controls, native
date/number pickers, links and unknown action outcomes do not qualify. This
narrows the eligible next choices; it never substitutes another action or treats
an unchanged visible snapshot as evidence of no external effects.

`tests/integration/test_scenario_guards.py` adds these browser regressions:

- Missing required text with no configured helper leaves fields untouched and
  sends no form request. The current terminal is `failed`; there is no new
  `needs_value` terminal or invented input.
- A textless overlay appears after target selection: neither the selected
  button nor the overlay receives a click.
- A visually identical replacement node appears during a decision: the old
  choice is discarded and the fresh node receives exactly one action.
- A native select changes location before its acknowledgement is lost: the
  server sees one mutation and one destination visit, the result is
  `outcome_unknown`, and the action is not replayed.
- A real access-denied page cannot pass an unmet report check by choosing DONE.
  An ordinary article containing the same words still permits its report link.
  This checks outcomes and avoids a false-positive block; it does not add a
  general site-wall detector.
- Iframe, Shadow DOM, new-window links, upload controls, canvas and password
  controls remain excluded from actions under the current contract, with no
  effects. An unoffered hover operation is rejected before execution.

`delayed_menu` waits 500 ms for a normal link to appear, then follows its observed
`href`. A late JS `pushState` after the runner's bounded post-action observation
remains a negative contract case in
`test_visible_dom_collection.py::test_runner_owns_only_bounded_same_document_transition[delayed_spa]`.
These tests do not enable new routing, frame, tab, shadow or hover capabilities.

The below-fold workflow exposed a coordinate-space bug in the existing click
guard. CDP DOM hit testing now adds the CSS layout viewport's document offset;
mouse input keeps its viewport coordinates. Native regressions cover horizontal
and vertical scrolling, device scale 1/2 and an overlay introduced after
observation. The frame, identity, freshness and recipient checks still apply.
See Chromium's [DOM hit-test implementation](https://github.com/chromium/chromium/blob/971a7443b0c9b0a9b2860529b33331b76077ec62/third_party/blink/renderer/core/inspector/inspector_dom_agent.cc#L1867-L1897)
and [input coordinate contract](https://github.com/chromium/chromium/blob/971a7443b0c9b0a9b2860529b33331b76077ec62/third_party/blink/public/devtools_protocol/domains/Input.pdl#L165-L177).

## Run

Use the environment and browser prerequisites in [verification.md](verification.md).
From the checkout, with its dependencies installed:

```sh
uv run python -m pytest -q tests/integration/test_browser_scenarios.py tests/integration/test_scenario_guards.py tests/unit/test_scenario_command.py
uv run python scripts/run_browser_scenarios.py --list
uv run python scripts/run_browser_scenarios.py --repetitions 3 --output /tmp/browser-scenarios-scripted.json
```

The ordinary full pytest suite collects these tests without a special marker.
The CLI defaults to scripted decisions and never implicitly selects credentials.
Reports use a new file, retain failed attempts and record the source hash,
limits, terminal outcomes and independent oracle. The CLI does not overwrite
earlier evidence or save provider exception text or raw response bodies.

To run the same synthetic tasks against the locally installed model:

```sh
DECISION_BROWSER_PROVIDER=local \
DECISION_BROWSER_ENDPOINT=http://127.0.0.1:11434/v1/systemone \
DECISION_BROWSER_MODEL=nimble:latest \
DECISION_BROWSER_MAX_CHOICES=26 \
uv run python scripts/run_browser_scenarios.py --provider configured --output /tmp/browser-scenarios-nimble.json
```

Other configured providers follow [providers.md](providers.md). The explicit
`configured` option invokes the normal production factory and can make billable
model calls. Only the declared goal and supplied values, normal observations and
verification checks reach the runner; expected event sequences and scripted
plans are not supplied to that provider. Defaults per task are 24 steps, 24
provider calls, 45 seconds and 256 KiB of provider input. Flags can lower these
bounds. This command records task results, not an invoice or complete HTTP
usage audit. Functional workflows and correct stopping cases are reported
separately; no old 18/18 live result is extended by scripted passes.

## Upstream references

These are independently authored fixtures inspired by pinned source tests:

- [jev-ultrafast `8251e272`: hotel smoke](https://github.com/browser-use/jev-ultrafast/blob/8251e272398011cdef648965204d1c20cd838333/scripts/smoke.py),
  [browser guards](https://github.com/browser-use/jev-ultrafast/blob/8251e272398011cdef648965204d1c20cd838333/scripts/check_guards.py),
  [interrupted action tests](https://github.com/browser-use/jev-ultrafast/blob/8251e272398011cdef648965204d1c20cd838333/tests/test_agent.py).
- [jev-ra `721fc73d`: autocomplete](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_suggestion_list.py),
  [form](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_e2e_fixtures.py),
  [calendar](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_agent_date_grid.py),
  [filters and consent](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_snapshot_overlay.py),
  [late menu routing](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_session_link_route.py),
  [late paint](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_session_late_paint.py),
  [below-fold content](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_snapshot_below_the_fold.py),
  [site walls](https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/tests/test_agent_wall.py).

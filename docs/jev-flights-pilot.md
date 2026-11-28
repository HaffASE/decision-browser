# Bounded Google Flights acceptance

Historical design record from the pre-migration implementation. Current native
commands and acceptance requirements are in [verification](verification.md).
The [original source and procedure](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/docs/jev-flights-pilot.md)
remain available at the pinned revision.


The approved fixture is JFK → LAX, 10 November 2026, one way, one adult,
economy including Basic. Search only: never select a flight or book.

## Order

1. Run the offline and full native verification profiles on a supported machine.
2. Review the source diff. Use the frozen dependency lock; do not disable Chromium
   sandboxing or substitute a model-driven mock for native tests.
3. Use an existing, explicitly authorized secure launcher to inject
   `OPENAI_API_KEY` into the child process. Do not print, copy, upload or put the
   key on the command line. This program does not read Keychain or key files.
4. Immediately before the first approved live attempt, run:

   `python -m decision_browser.flights_pilot --ledger /absolute/path/flights-experiment.jsonl --output /absolute/path/flights-attempt-1.json --initialize`

   Use the installed package or `PYTHONPATH=src` in a source checkout. The browser
   is isolated native Playwright Chromium on the selected machine.
5. Any authorized retry uses the SAME ledger and a NEW output file, WITHOUT
   `--initialize`. Do not replace or recreate a ledger to replenish limits.
   Initialization starts the first fixed 600-second wall-clock window. An expired
   window requires the separate operator procedure below; ordinary run cannot renew.

## Bounds and cost

40 Decisions calls, 40 action attempts, 512 KiB cumulative serialized input,
600 seconds per separately authorized window, including idle/restart time; no images,
no Responses fallback and no automatic
HTTP retry. Each request and attempt is reserved durably before dispatch.
Missing, corrupt, replaced or concurrently opened ledgers fail closed.

The approved spending ceiling is $0.25 for this one experiment. Application cost
estimation uses the verified standard Decisions rate of $0.10 per million input
tokens, one estimated token per serialized UTF-8 byte, 4096 overhead tokens per
call, and $0.05 uncertainty reserve. This is a conservative application estimate,
not billing or a server-enforced dollar cap. Do not run if the account/model
pricing materially differs; obtain a new decision first.

## Additional windows, with the same cumulative budget

This procedure needs explicit human authorization for additional time. It does
not imply permission from an expired window, a failed run, or unused dollars.
The operator must retain the SAME original ledger. Calls, attempts, bytes,
reported usage and unknown-call reservations stay cumulative across every window;
the original $0.25 estimate ceiling and $0.05 uncertainty reserve do not replenish.
The journal supports at most 40 windows including the first, each at most 600
seconds. A finished or exhausted experiment cannot get another window.

All operator commands work offline and do not inspect keys, Keychain or providers.
Use `env -u OPENAI_API_KEY python -m decision_browser.experiment_operator status
--ledger /absolute/path/flights-experiment.jsonl` for a read-only report.

After human approval, immediately before an attempt:

1. Create a NEW intent file with `env -u OPENAI_API_KEY python -m
   decision_browser.experiment_operator plan-window --ledger /absolute/path/flights-experiment.jsonl
   --seconds 600 --output /absolute/path/window-2-intent.json`. This reads the
   ledger and writes a proposal only. It grants no authority and never overwrites
   an existing intent. Review its fixed `not_before/not_after`, UUID, expected
   SHA-256, ledger identity and cumulative report. The absolute deadline is fixed
   when the intent is generated, so waiting consumes available time.
2. The explicitly authorized operator runs `env -u OPENAI_API_KEY python -m
   decision_browser.experiment_operator authorize-window --ledger /absolute/path/flights-experiment.jsonl
   --intent /absolute/path/window-2-intent.json --operator-authorized` while that
   intent remains valid. The flag records an operator assertion; it is not a
   cryptographic proof of a human click and must never be inferred by ordinary run.
3. Resume `decision_browser.flights_pilot` with that SAME ledger and a NEW report,
   WITHOUT `--initialize`, using the authorized secure user-input launcher.

The first authorization migrates v1 by appending a `window_authorized` record with
the fixed intent. The header and every previous byte remain unchanged. Replay
checks each authorization against the exact raw prior-prefix SHA-256 and each
historical call/attempt against the window that was active then. Status and plan
open read-only; operator and runner share an exclusive process lock. In-place
tampering, missing/replaced files and concurrent use fail closed. v2 also binds
authorizations to the local filesystem device/inode; copying, replacing, remounting
or moving to another machine can block resume. Never automatically rebind it.

Write/fsync uncertainty poisons the current handle: stop without dispatch or an
automatic retry. A surviving complete record is counted once on reopen; duplicate
UUIDs and stale expected hashes cannot append. If an unconfirmed record was lost,
the SAME original fixed intent can only retain its earlier absolute deadline, not
extend it. A partial/corrupt tail stops resume with no truncation or repair. An
expired intent cannot be revived: another intent requires a separate authorized
operator decision within the human-approved time policy. Never delete/reset the
ledger, regenerate intents automatically or launch an unbounded retry loop.

The report separates original start/deadline, current window/deadline, number and
total duration of authorized windows, and cumulative estimated/observed expenditure.

## Evidence

The production RunManager performs observation, guard, admission, native dispatch
and fresh observation. Decisions selects the operation and observed target.
The pilot adds only restrictions, cumulative budgeting, and read-only evidence.
Its compact prompt references shared elements without pruning the choice domain.

PASS requires acknowledged Search tied to the pre-search query, an acknowledged
calendar date including year, matching selected airports/trip/class/adult state,
a result-associated route/date summary, changed and settled result rows, and
carrier, departure, arrival, price and currency from one visible real result row.
The accepted Search must start from the blank form without preexisting result evidence.
No price is copied from another row. A correct form with stale results fails.
A manually driven browser session and mocked tests cannot satisfy this gate.

Output uses exclusive creation and contains a safe task result, acknowledged
trace (including partial/unknown attempts), exported sanitized journal, exact package
source hashes before/after the run, query-bound oracle report and estimated/observed usage. It contains no
credential, request authorization header or provider raw error text. A failed
attempt remains NOT_VERIFIED, never a successful search.

At the stale-refresh boundary, a rejected transition adds one stable code to
`result.evidence`: `stale_refresh:document_changed`,
`stale_refresh:observation_url_changed`, or `stale_refresh:live_page_url_changed`.
The first failed comparison wins in that order. No compared Document IDs, URLs,
page text or exception details are added. The public failure code remains
`navigation`; guards, admission and dispatch behavior are unchanged.

These codes locate a rejected comparison; they do not prove its cause or grant
recovery. Reports produced before these diagnostics cannot establish which
comparison failed. A same-origin/same-document transition after an acknowledged
action still needs the existing bounded action ownership checks. Recovery must
discard the stale action and obtain a fresh observation and new decision within
the original scope and budget; it requires separate evidence and review before
implementation. Never infer a delayed SPA transition from `navigation` alone.

During an acknowledged action, an already allowed same-document SPA transition
can race the post-action capture: the live URL has changed but the captured
observation still has the old URL. Only in that case, one corrective observation
must keep the original Document and origin, match the already accepted URL, and
still match the live URL with no popup. Failure remains `outcome_unknown`, keeps
the consumed attempt, and exports a post-observation failure journal entry.
Success adds `post_observation:owned_url_reobserved`. It does not repeat the
action or provider call, extend the deadline, or let stale refresh adopt a later
URL change. A matching synthetic race does not prove the historical run's timing.

The pilot exports the last 40 `diagnostics` records for choose, failed dispatch,
and stale refresh. Fixed outcomes and nullable URL-alignment booleans compare
the observation to the live URL at decision start and end; refresh records its
fixed reason and trigger.
`acknowledged_actions` counts only the current pilot's acknowledged UI trace,
excluding initial navigation and prior windows. A successful action trace copies
the core's boolean `page_changed` and reports nullable post-observation Document,
origin, URL-change and live-URL relations. These metadata add no observations,
provider calls or recovery permissions and cannot establish older runs' timing.

## Historical query and report boundary

The immutable `flights_query` contract owns the airport identities and departure
2026-11-10. Historical fixture evidence can still be validated offline. The live
`run_pilot` entry refuses `expired_query` against the UTC calendar before key
lookup, provider construction, initial navigation reservation or provider work.
A current live query needs an explicitly updated contract and matching fixture
corpus; changing dates is not an automatic retry or renewed authorization.

Calls and UI attempts have separate capacity checks immediately before new work.
The last permitted response remains usable, and final read-only evidence can run
at the call/step boundary while the existing deadline, admission, document and
navigation-generation guards continue to apply. Host chronology is compared to
browser timestamps at their common millisecond precision; within-millisecond
ordering still comes from the host's sequential capture path, not wall time.

Partial failure reports retain the already collected projected actions,
diagnostics, source and ledger identities. Cleanup failures are independent
closed codes and prevent PASS; a primary `outcome_unknown` keeps priority.
These JSON reports are local diagnostic exports, not a crash-durable audit store
or authenticated envelope. Existing source digests, ledger digest/identity and
hash-only fingerprint projections remain. An aggregate seal is not added: an
unkeyed checksum would neither authenticate the producer nor prove completeness.
Any future durable/authenticated export contract needs its own requirements and
verification.

The trusted compact Flights context has a closed root, candidate references,
state flags, node/form/context/history types and supported operation grammar.
The legacy non-continuation manager can describe the existing ordinary finite
operation domain; select option arrays/choices have explicit types. The
continuation manager still filters its narrower pilot offer set before provider
work. Schema acceptance grants no dispatch authority. Page strings and
`observation_diagnostics` are bounded, finite, untrusted descriptive JSON;
observation diagnostics grant no action or success authority. The privacy gate
checks source values before the trusted marker branch, which validates the
compact structure and ordinary marked choice domain again.

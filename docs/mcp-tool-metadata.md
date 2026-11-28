# MCP tool discovery contract

The six `tools/list` definitions describe the existing protocol. They do not add
an approval mechanism or change request validation, execution limits or responses.

- `start_task` starts real asynchronous work. Recover a lost response with
  `get_task` without an ID; never automatically start a second task.
- `get_task` polls a known run or recovers the single retained run. A missing or
  expired run is not proof that no action happened.
- `present_admission` needs the exact three identifiers from the same current
  `pending_proposal`. It presents a supported client form; a call is not a grant,
  and a host response is not proof of a native human click.
- `cancel_task` requests cancellation and revokes admission. Poll for finalization;
  `cleanup_unknown` is not a successful cleanup receipt or rollback.
- `read_artifact` takes server-issued IDs and original-byte ranges. Explicit
  structured/resource channels paginate using `next_offset` until `eof`.
- `capabilities` reports prerequisites, not live or production readiness.

Read tools declare `readOnlyHint=true` and `openWorldHint=false`. Start, admission
and cancellation conservatively declare destructive/open-world behavior. Only
cancellation declares idempotency: repeating it on the same retained run does not
start more work. These annotations are client hints, never authorization.

Success `outputSchema` definitions use the pinned MCP SDK's
`Annotated[CallToolResult, TypedDict]` support. The SDK validates structured content
but returns the original result object: it does not wrap the response or duplicate
resource data. Domain errors still have `isError=true` and `{"error": code}` and
are not constrained by the success schema. Artifact fields that only exist for
particular channels are optional; their descriptions explain the channel contract.

## Rust discovery and native stdio

The Rust candidate serves the six-tool discovery captured from PR #10.
The native stdio tests exercise strict argument validation, retained-run
recovery, proposal binding and the three artifact channels. The isolated
Codex App Server smoke verifies real `start_task`, `get_task`,
`present_admission` and `read_artifact` on an owned browser fixture.

- [Frozen discovery schemas](../prototypes/rust-cdp-counter/contracts/generated-tools.json)
- [Native Codex smoke](../prototypes/rust-cdp-counter/fixtures/native-codex-smoke-2026-11-28.json)

## Verification

`tests/unit/test_mcp_tool_metadata.py` connects through the real
`python -m decision_browser.cli serve` entry point in both admission modes with an
empty synthetic provider profile. It validates the advertised schemas, annotations,
capabilities and errors without launching a browser or calling a model. Additional
synthetic-store checks validate all three artifact envelopes and run recovery.
Existing server and wave-4 contract tests cover strict inputs, pending proposals,
pagination, fixed errors and cancellation behavior.

For deterministic lint, use the [official TDQS CLI](https://tdqs.dev/cli) against
that entry point with a prepared project Python and an absent synthetic profile:

```sh
DECISION_BROWSER_CONFIG_FILE=/absolute/path/to/absent-test-profile.json \
  npx --yes mcp-tdqs@0.2.0 lint \
  --command '.venv/bin/python -m decision_browser.cli serve --admission standard_form' \
  --format json
```

A local comparison on 2026-11-27 used actual stdio `tools/list` captures from
base `29710249b1d928ce17bcd7acb35ff99cb23aaf66` and this change. With mcp-tdqs 0.2.0,
spec 1.3, warnings decreased from **12 to 1**: six missing-annotation warnings and
five undocumented-parameter warnings were removed. All exposed parameters have
schema descriptions. Input schemas are unchanged after removing description/title
metadata; required fields, defaults, patterns and bounds are identical.

The remaining `shadow-candidate` warning compares the invocation cost of
`start_task` (17) and `present_admission` (3). It is a heuristic, not evidence these
tools are interchangeable: their lifecycle roles and prerequisites are now explicit.
Do not weaken required inputs or combine these operations to suppress that warning.
No model-based score, paid evaluation, Glama rescore or agent-choice quality
measurement was performed. Lint is not a TDQS letter grade.

Local validation on macOS / Python 3.13: **3152 unit tests passed**, including the
new real-CLI tests; **5 existing stdio contract tests passed** (4 browser/model-stop
scenarios intentionally not selected). Ruff passed and mypy passed for all 46 source
files. The first sandboxed unit attempt was superseded: local socket/process checks
were restricted, a borrowed environment could not import the new module under `-I`,
and one existing case-sensitive description assertion needed the established phrase
"Standard MCP form". The final run used this checkout’s own locked environment.
No full browser suite or native-client human approval was exercised.

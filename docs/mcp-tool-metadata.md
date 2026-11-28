# MCP tool discovery

The six `tools/list` definitions preserve the public protocol's descriptions,
argument types, annotations and response schemas. The runtime validates the
same advertised inputs before work starts.

- `start_task` starts one asynchronous run. Recover a lost response with
  `get_task` without an ID and retain the current run.
- `get_task` polls a run or recovers the single retained run. A missing or expired
  run provides insufficient evidence about prior browser effects.
- `present_admission` uses the exact run ID, proposal ID and digest from the
  pending proposal. The client presents its scope form to the user.
- `cancel_task` revokes admission and requests cancellation. Poll until cleanup
  reaches a terminal result.
- `read_artifact` reads server-owned evidence with bounded whole or ranged
  envelopes and SHA-256 checksums.
- `capabilities` reports modes, actions, provider prerequisites and policy.

The server preserves legacy, structured and resource artifact channels. Each
ranged read reports total size, offset, next offset, EOF and chunk checksum.
The six checked-in definitions come from the migration's captured discovery.
[Discovery](../contracts/generated-tools.json) · [Native validator](../src/tool_contracts.rs)

## Verification

```sh
cargo test --locked --test product_mcp
cargo test --locked --lib tool_contracts
cargo test --locked --lib product_mcp
```

The tests cover strict argument types, retained-run recovery, exact proposal
binding, form refusal/expiry, cancellation and artifact envelopes. The
[Codex cutover evidence](../fixtures/native-codex-cutover-2026-11-28.json) records
live counter acceptance, an existing disposable Chrome and public HTTPS
navigation through the client transport. Programmatic form responses are
protocol evidence. Record native user-click evidence separately.

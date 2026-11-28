# MCP compatibility

Decision Browser implements JSON-RPC stdio MCP in Rust. The server advertises
protocol version `2025-11-25` and six tools. Its checked-in discovery schemas
preserve the public input and output fields.

Arguments use strict JSON types and reject unknown fields. A JSON-encoded object
inside a string fails validation. An omitted or JSON-null `get_task.run_id`
recovers the current retained run. Tool domain errors use `isError: true` and the
fixed `{"error": "code"}` envelope.

Standard-form admission uses `elicitation/create`. The client must support form
elicitation. Each response binds its form ID, pending proposal, digest and
expiry, with strict `reviewed: true` content. Decline, cancel and expiry retain
their terminal reason after worker cleanup. URL-only clients receive an explicit
unsupported-grant result.

Run native stdio and lifecycle tests after changing the protocol.

```sh
cargo test --locked --test product_mcp
cargo test --locked --test protocol
```

The [Codex acceptance example](../examples/codex_acceptance.rs) exercises the
actual client transport and programmatically responds to forms. Native user
interaction requires a separately observed client acceptance test.
[Discovery schemas](../contracts/generated-tools.json)

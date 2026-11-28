# Native host acceptance

Run the local native suite and package lifecycle from [Verification](../../docs/verification.md). The programmatic Codex App Server test additionally exercises stdio discovery, Standard MCP forms, native input, independent completion checks, and artifact reads through a real host. Its form answers are test responses. Record human accept, decline and cancel separately when validating a client's form UI.

```sh
cargo build --locked --release --bin decision-browser
cargo build --locked --example codex_acceptance
target/debug/examples/codex_acceptance target/release/decision-browser /tmp/native-counter-jev jev --product
```

Use an empty evidence directory for each invocation. The counter requires exactly three increments in its own local server. Use the six tools advertised by `capabilities` and discovery. An unknown action outcome requires checking the page before considering a repeat.

For an existing-mode regression use an isolated, disposable Chrome profile with a `DevToolsActivePort` file and set `DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT` to its absolute path. Pass `--product-existing` instead of `--product`. The local Chrome suite separately checks foreign tab preservation and same-profile cookie access. A real user's signed-in browser is outside these test fixtures.

When validating a different host, install its project registration and record separately the client version, artifact SHA-256, connection, human form outcomes, browser verification, cancellation, and cleanup. Keep credentials, cookies and private content out of the evidence.

# Rust CDP counter prototype

The prototype exposes one stdio MCP tool, `run_counter`. After a form response with `action: "accept"` and `reviewed: true`, it starts a loopback fixture and opens installed Chrome in a private temporary profile. Rust observes the counter over CDP, obtains one bounded decision, checks the button again, dispatches one mouse press and release, reads `0 → 1`, and returns a PNG. The tool reports success after the owned browser has shut down.

The runtime uses a native Rust binary and Chrome. Set `DECISION_BROWSER_CHROME` to an absolute Chrome executable path when installation discovery is insufficient. The default `fixture` provider makes no network request. The `jev` and `luna` providers send the synthetic counter observation and three fixed action choices to their respective HTTPS APIs. They load `TYPESAFE_API_KEY` and `OPENAI_API_KEY` from macOS Keychain. Each accepted live call makes at most one request and reports the returned model and token counts when available.

## Build and check

Run these commands in the prototype directory.

```sh
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked --all-targets
```

## Run through Codex

The acceptance harness starts the installed `codex app-server` with the Rust binary registered for the test invocation, creates an ephemeral thread, and calls `run_counter` directly. It declines the first real MCP form, accepts a second form, and checks the counter result and PNG. The harness supplies the form responses programmatically. Its evidence covers Codex's MCP host and the Chromium click. Desktop/TUI rendering and human input require a separate run.

```sh
target/debug/examples/codex_acceptance target/debug/decision-browser-rust-cdp /tmp/rust-cdp-fixture fixture
target/debug/examples/codex_acceptance target/debug/decision-browser-rust-cdp /tmp/rust-cdp-jev jev
target/debug/examples/codex_acceptance target/debug/decision-browser-rust-cdp /tmp/rust-cdp-luna luna
```

The published six-tool interfaces are the next porting surface. The pinned [PR #10 baseline](contracts/pr-10.json) captures source declarations from the PR head for comparison with the future Rust `tools/list` output.

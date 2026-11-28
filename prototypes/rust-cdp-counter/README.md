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

## Timed live browser matrix

The native matrix runner serves the checked-in fixtures in Rust, scores
browser state and independent server-side events, and writes one JSON line
per attempted case. The browser uses an isolated Chrome profile. All 21 cases
are selected when the command is called without case names.

```sh
cargo build --release --locked --bin browser_matrix
target/release/browser_matrix --list
target/release/browser_matrix jev /tmp/decision-browser-rust-jev.jsonl
target/release/browser_matrix luna /tmp/decision-browser-rust-luna.jsonl
target/release/browser_matrix gemini /tmp/decision-browser-rust-gemini.jsonl
```

Use fresh evidence paths. Gemini Decision uses the macOS Keychain service
`codex-opencode-console` and the OpenCode System One endpoint. The other two
routes use the same services as the counter. Each run uses the published case
goals, fixture data, checks and limits. The Rust action policy and CDP driver
have their own implementation. `elapsed_ms` covers the fixture, browser,
model, verification and owned browser cleanup. The per-case report includes
failures so that elapsed time and confirmation rate can be read together.

The public `public_about` case sends observed Python.org page text to the
selected model. Other cases use synthetic loopback pages. The matrix exercises
the isolated case runner. The separate Codex example
exercises MCP form elicitation and a native counter action.

### Live results on 28 November 2026

The complete matrix recorded one measured attempt in each of 63 cells.
The optimized Rust binary used Chrome for Testing 151.0.7922.34 on macOS arm64.

| Live model | Confirmed | Median elapsed | Median provider time |
| --- | ---: | ---: | ---: |
| Jev 1.14.0 | 20/21 | 5.828 s | 1.886 s |
| Decision Luna | 19/21 | 5.160 s | 1.761 s |
| Gemini Decision 1 | 19/21 | 5.131 s | 1.872 s |

The unconfirmed cases were `article` for Jev, `loading_overlay` and
`rail_itinerary` for Luna, and `loading_overlay` and `article` for Gemini.
Medians include unconfirmed attempts. The earlier Python run on the same
Chromium build recorded 3.687 s, 3.722 s, and 4.455 s, respectively. The Rust
and Python implementations use different action policies and runners, so
these timings describe the complete pipelines. [Per-case timings and outcomes](fixtures/timed-live-results-2026-11-28.json)

After the full run, the runner was changed to wait locally when the page
offers no browser actions. The two targeted `loading_overlay` repeats passed
on Luna (6.790 s) and Gemini (6.750 s). The table still shows the original
complete matrix.

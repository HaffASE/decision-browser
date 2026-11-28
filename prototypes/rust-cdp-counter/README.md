# Rust CDP counter prototype

The original counter binary exposes one stdio MCP tool, `run_counter`. After a form response with `action: "accept"` and `reviewed: true`, it starts a loopback fixture and opens installed Chrome in a private temporary profile. Rust observes the counter over CDP, obtains one bounded decision, checks the button again, dispatches one mouse press and release, reads `0 → 1`, and returns a PNG. The tool reports success after the owned browser has shut down.

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

The pinned [PR #10 baseline](contracts/pr-10.json) and [captured discovery](contracts/generated-tools.json) record the six-tool protocol used by the native candidate.

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

### Second complete run

Each route attempted all 21 cases on the same Chrome for Testing build. The
three routes ran concurrently on the test machine. Medians include every
attempt; use the first run for the original comparison with Python.

| Live model | Confirmed | Median elapsed | Median provider time |
| --- | ---: | ---: | ---: |
| Jev 1.14.0 | 20/21 | 5.721 s | 1.615 s |
| Decision Luna | 20/21 | 4.721 s | 1.464 s |
| Gemini Decision 1 | 20/21 | 5.863 s | 1.990 s |

`article` exhausted its budget on Jev and Gemini, and `rail_itinerary` exhausted
its budget on Luna. After the runner stopped repeating actions in unchanged
states, targeted reruns passed on Jev (6.089 s) and Luna (11.852 s). A Gemini
rerun hit HTTP 400. The runner then handled a single available fixture action
locally, and the Gemini case passed in 6.149 s. [All 63 measured attempts and targeted reruns](fixtures/live-matrix-followup-2026-11-28.json)

## Six-tool native candidate

Build the native CLI with `cargo build --release --locked --bin decision-browser`.
It registers the six-tool MCP server using `decision-browser serve`. The
one-tool counter binary remains available as a separate regression entry point.

The acceptance example's `--product` option tests the six-tool process
through an isolated Codex App Server. It uses an owned loopback counter and a
live provider. The test client accepts the forms programmatically and reads
the JSON check report through `read_artifact`.

The optimized binary on macOS completed three verified clicks following four
forms on each route. From `start_task` through reading the report, Jev took
5.926 s, Decision Luna 6.443 s, and Gemini Decision 6.659 s.

```sh
cargo build --locked --bins --examples
target/debug/examples/codex_acceptance target/debug/decision-browser /tmp/native-codex-jev jev --product
```

[Native package and install](../../docs/native-installation.md) ·
[Codex counter results](fixtures/native-codex-smoke-2026-11-28.json)

The native provider configuration supports six fixed presets. The MCP runner
accepts trusted loopback fixtures at `http://127.0.0.1` in `isolated` mode.
CDP Fetch blocks cross-origin HTTP requests from the attached page before they
reach the network. Coverage includes the tested form, frame, redirect and
subresource paths. Workers, new targets and non-HTTP browser transports still
need separate coverage.
The separate `existing_chrome` CDP regression uses a trusted local DevTools port
file and preserves a foreign tab on a disposable Chrome instance. Visual
diagnostics keep bounded geometry locally and report an incomplete visual result.

The release still requires the complete legacy policy and test migration.
The migration still covers policy for general browser tasks, authenticated
Chrome, automatic browser egress, task-scoped effectful input, text helpers,
custom provider routes and legacy reboot-witness recovery. The native installer
can migrate exact managed registrations, check the legacy install lease and
roll back its own journal.

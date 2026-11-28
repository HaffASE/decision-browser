# Browser scenarios

The native matrix runner serves the checked-in loopback fixtures, uses a real
sandboxed Chrome through CDP, and verifies both page checks and server-recorded
events. The public `public_about` case follows the About link on Python.org.
The corpus retains 21 original goals, values, checks and finite limits.

```sh
cargo build --locked --release --bin browser_matrix
./target/release/browser_matrix --list
./target/release/browser_matrix jev /absolute/path/new-jev.jsonl
./target/release/browser_matrix luna /absolute/path/new-luna.jsonl
./target/release/browser_matrix gemini /absolute/path/new-gemini.jsonl
```

A case name after the output path selects a targeted run. Output paths must be
fresh. Every attempted case writes a row with confirmation, elapsed time,
provider time, observed actions, checks, event oracle and error. Keep failures
when summarizing timing. List reruns as subsequent attempts.

The matrix is a standalone native runner. The Codex product test adds the MCP
start, polling, approval and artifact lifecycle. [Verification](verification.md)

## Recorded results

The Rust cutover run attempted all 63 cells on macOS arm64 with Chrome for
Testing 151.0.7922.34. The three routes ran concurrently. Medians cover every
first attempt, including browser cleanup and API failures.

| Route | First attempts confirmed | Median elapsed | Confirmed including reruns |
| --- | ---: | ---: | ---: |
| Jev | 16/21 | 5.516 s | 21/21 |
| OpenAI Decision Luna | 13/21 | 8.941 s | 21/21 |
| Gemini Decision | 16/21 | 5.730 s | 21/21 |

Sixteen first attempts reached the page and event checks but could not confirm
process-group cleanup. Two stopped on provider errors. Targeted repeats confirmed
each remaining cell. The last Luna `equipment_pickup` repeat passed in 12.473 s
after the owned Chrome process-group probe fix. [Cutover attempts and reruns](../fixtures/rust-cutover-matrix-2026-11-28.json)

### Earlier Rust candidate

The second 28 November 2026 run confirmed 60 of 63 cells. Jev and Gemini reached
the limit on `article`, and Luna reached it on `rail_itinerary`. Targeted reruns
after progress fixes confirmed the remaining cases. The evidence retains the
first failures and a Gemini HTTP 400 encountered during a rerun.
[All attempts](../fixtures/live-matrix-followup-2026-11-28.json)

Historical series under `docs/benchmarks` retain the original dates, browser
versions, provider routes and outcome rules. The migration's copied source files
are available in [the pinned Git tree](https://github.com/HaffASE/decision-browser/tree/7babeb4114130e1bf97bd0f23599405b70965372/docs/benchmarks/2026-11-17-browser-matrix/capture-source).

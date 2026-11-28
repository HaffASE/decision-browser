<h1 align="center">Decision Browser</h1>

Decision Browser is a Rust stdio MCP server for Codex and Claude Code. Give your
agent a URL, a goal, field values and final checks. The server observes browser
controls, selects bounded actions through the configured model, rechecks each
target through CDP and returns a verifiable result.

[Download v0.2.1](https://github.com/HaffASE/decision-browser/releases/tag/v0.2.1) ·
[Installation](docs/installation.md) · [MCP tools](#mcp-tools) ·
[Providers](docs/providers.md) · [Verification](docs/verification.md) ·
[Release procedure](docs/releasing.md)

## Quick start

Release archives are available for macOS arm64 and Linux x86-64. Check the
archive against the release's `SHA256SUMS`, then extract it. Keep Chrome or
Chromium installed with its sandbox enabled. Run the executable from your project.

```sh
/absolute/path/to/decision-browser/bin/decision-browser setup codex
/absolute/path/to/decision-browser/bin/decision-browser configure openai
```

Choose `claude` or `both` for the other client registrations. `setup` installs
project-local MCP settings and a skill. `configure` selects the provider preset.
Supply credentials separately using the [provider guide](docs/providers.md).
Use `install codex --project /absolute/path/project` for unattended registration.

Restart the coding client, review its trust and MCP prompts, and ask it to call
`capabilities`. Installation, API access, client approval and verified browser
execution each have their own checks. Use the executable's `doctor --json`
command to inspect local installation prerequisites.

A source build requires Rust 1.100.0 or newer and Cargo.

```sh
cargo build --locked --release --bin decision-browser
./target/release/decision-browser install codex --project /absolute/path/project
```

The native archive runs without Python, uv or Node.js. A source build downloads
the dependencies pinned by Cargo.lock when they are absent from the local cache.
Chrome and the coding client are separate prerequisites. See the
[installation guide](docs/installation.md) for configuration, upgrades and recovery.

### Install through npm

With Node.js 20 or newer, run the published package from your project.

```sh
npm exec --yes --package=@haffa/decision-browser@0.2.1 -- decision-browser setup codex
npm exec --yes --package=@haffa/decision-browser@0.2.1 -- decision-browser configure openai
```

The package includes the Rust executables for macOS arm64 and Linux x64 and
verifies the matching executable before launch. `setup` copies it into the
project's persistent runtime. Install Chrome or Chromium separately and supply
provider credentials as described above.

### Upgrade an existing project

Close the coding client and run `upgrade` using the executable from the new
archive. Select the same client or clients as the old registration.

```sh
/absolute/path/to/decision-browser/bin/decision-browser upgrade codex --project /absolute/path/project
/absolute/path/to/decision-browser/bin/decision-browser doctor --json --project /absolute/path/project
```

Version 0.2 marks the compatibility break from the Python distribution. The
native installer can migrate recognized, unchanged project registrations.
For custom MCP registrations, use the native executable with `serve` and follow
the [installation guide](docs/installation.md).

## Browser tasks

Supply a URL you are authorized to use, an exact goal and independent checks.
For example, ask the agent to choose Zurich Airport from an autocomplete and
verify the selected field value. Provide known form text in `values`.

`isolated` mode creates a fresh browser context. `existing_chrome` uses an
acknowledged task tab in a selected Chrome debugging session and shares that
session's account access. Use it with `standard_form` and a Chrome session with
remote debugging enabled. Task navigation and requests follow the agreed origin,
which can block cross-origin resources and transitions. Check `capabilities`
before selecting a mode. [Browser control](docs/browser-control-stage-one.md)

The runner enforces the request's step, provider-call, byte and elapsed-time
limits. Browser inputs operate on observed controls. Functional verification
uses the requested page checks. Visual diagnostics are reported separately.

## MCP tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `capabilities` | None | Modes, actions, provider prerequisites and admission policy |
| `start_task` | `request` | Starts one retained asynchronous task and returns its run ID |
| `get_task` | `run_id?` | Polls a run or recovers the single retained run after a lost start response |
| `present_admission` | `run_id`, `proposal_id`, `digest` | Asks the client to show the pending scope form |
| `cancel_task` | `run_id` | Revokes admission and requests cancellation |
| `read_artifact` | `run_id`, `artifact_id`, optional channel and range | Reads bounded evidence by server ID |

Start once and poll `queued`, `awaiting_human_admission` and `running` results.
When a pending proposal exists, pass its exact identifiers to `present_admission`
and let the user answer the client form. Approval binds the displayed scope,
expiry and semantic target. Read a terminal result's reason and recovery fields.
An `outcome_unknown` result requires inspecting the outcome before deciding
whether another run is safe.

The server retains its run in memory. Recover a lost start reply with `get_task`
without a run ID. Restarting the server loses that state. A missing retained run
provides insufficient evidence to repeat a potentially completed action.

The artifact channels are `legacy`, `structured` and `resource`. The default
`legacy` envelope carries whole artifacts up to 65,536 bytes. Use `structured`
or `resource` with `offset` and `limit` for larger artifacts. Verify each chunk's
hash and the final artifact SHA-256. [Wire schemas](contracts/generated-tools.json)
· [Agent workflow](skills/decision-browser/SKILL.md)

## Providers

Choose a provider with `decision-browser configure <provider>`. Credentials and route
settings belong in operator configuration. The MCP task arguments contain the
browser goal and values.

| Provider | Route | Credential |
| --- | --- | --- |
| `openai` | OpenAI Decisions | `OPENAI_API_KEY` |
| `typesafe` / `jev` | TypeSafe System One | `TYPESAFE_API_KEY` |
| `openrouter` | OpenRouter Decisions | `OPENROUTER_API_KEY` |
| `opencode` | OpenCode Console System One | `OPENCODE_API_KEY` |
| `gemini` | Gemini Decision through OpenCode Console | `OPENCODE_API_KEY` |
| `local` / `nimble` | Loopback System One service | Optional operator configuration |

On macOS, supported routes can load named Keychain credentials. Saved profiles
use private files outside projects. `capabilities` reports configuration
prerequisites. A successful API task establishes access for its recorded route
and model. [Provider configuration](docs/providers.md)

## CLI

| Command | Purpose |
| --- | --- |
| `setup [codex\|claude\|both]` | Register the selected client, prompting for a client when omitted in a terminal |
| `install codex\|claude\|both` | Register the native server in the selected project |
| `configure <provider>` | Save the selected provider preset |
| `serve [--admission standard_form\|task_scope]` | Serve MCP over stdio |
| `doctor --json` | Inspect local prerequisites |
| `upgrade codex\|claude\|both` / `uninstall codex\|claude\|both` | Update or remove owned project registrations |
| `install-status` / `recover-install` | Inspect interrupted installation and recovery |
| `probe-browser [--existing]` | Verify CDP against an owned local fixture |

Use `--project /absolute/path/project` for installation commands. Edited client
settings or managed skills stop conflicting changes. The recovery preview
reports the retained state, and `recover-install --apply` performs eligible
recovery. See [installation](docs/installation.md).

## Boundaries

Use authorized sites and minimally privileged accounts. Existing Chrome shares
its selected account context. Page content is untrusted, and selected providers
receive the bounded page context needed for decisions. Sensitive controls and
blocked literals have local checks. Review evidence before sharing it.

Recognized purchase, send, publish, delete, transfer and booking actions are
blocked. Ordinary controls can autosave or trigger other site effects. Target
validation, admission and same-origin request controls constrain supported work.
Browser mechanisms outside the advertised action set stop with a reason.

Chrome's sandbox stays enabled. Functional checks determine task success.
Geometry diagnostics can report `incomplete` and do not establish visual success.
[Security](SECURITY.md) · [Action policy](docs/action-classification.md)

## Native validation

The original Rust cutover was exercised through Codex App Server with an optimized
native executable on macOS arm64. The recorded times include `start_task`, the host's
programmatic form responses, the browser actions and the artifact read.

| Browser check | Decision route | Elapsed |
| --- | --- | ---: |
| Isolated counter with three verified clicks | Jev | 6.120 s |
| Isolated counter with three verified clicks | OpenAI Decision Luna | 6.260 s |
| Isolated counter with three verified clicks | Gemini Decision | 8.206 s |
| Counter in an existing disposable Chrome | Jev | 4.520 s |
| Public HTTPS About navigation | Jev | 3.855 s |

The existing browser retained its original tab. The public navigation verified
the exact destination. [Codex cutover evidence](fixtures/native-codex-cutover-2026-11-28.json)

The standalone browser matrix and its live timing reports retain failed
attempts and targeted reruns. [Browser scenarios](docs/browser-scenarios.md)


## Benchmarks

### Browser tests: 17 November 2026

The baseline snapshot ran all 21 published cases three times on each route:
189 real browser attempts, including every failed attempt. Twenty cases use
synthetic fixtures; `public_about` follows Python.org's public About link.

| Agent and route | Confirmed | Physical API calls | Reported input tokens | Reported output tokens |
| --- | ---: | ---: | ---: | ---: |
| Decision Browser · TypeSafe Jev `jev-1.14.0` | 13/63 | 530 | 1,810,309 | 161,088 |
| Decision Browser · OpenAI Decisions `gpt-6-luna` | 52/63 | 293 | 738,574 + unknown | 0 + unknown |
| Standalone Terra · `gpt-6.1-terra`, medium | 57/63 | 384 | 390,682 | 9,744 |

One Decisions call returned HTTP 503 and its usage was not captured. Its token
consumption is unknown and excluded from the reported partial totals. No attempt was retried.
Jev used TypeSafe directly, with `jev-latest` resolving to `jev-1.14.0`.
Confirmation requires native completion, independent page and event checks,
cleanup, and complete reported input/output usage. All sessions were isolated;
Core used development `task_scope` admission.

[All case results, errors, token details and source snapshots](docs/benchmarks/2026-11-17-browser-matrix/report.md)
· [189 audited attempts (CSV)](docs/benchmarks/2026-11-17-browser-matrix/audited-runs.csv)
· [Audit and reporting correction](docs/benchmarks/2026-11-17-browser-matrix/audit.json).

#### Failure rerun

The updated snapshot reran each of the 67 unconfirmed attempts once with the
same goals and limits. The last column combines the retained baseline successes
with confirmations from the rerun.

| Agent and route | Baseline confirmed | Confirmed in rerun | Retained + recovered |
| --- | ---: | ---: | ---: |
| Decision Browser · TypeSafe Jev `jev-1.14.0` | 13/63 | 9/50 | 22/63 |
| Decision Browser · OpenAI Decisions `gpt-6-luna` | 52/63 | 7/11 | 59/63 |
| Standalone Terra · `gpt-6.1-terra`, medium | 57/63 | 6/6 | 63/63 |

[Per-case results, timings, token usage and remaining errors](docs/benchmarks/2026-11-17-browser-remediation/report.md)
· [Rerun attempt CSV](docs/benchmarks/2026-11-17-browser-remediation/audited-runs.csv).

#### OpenCode Console

Two fresh series ran all 21 cases with three repetitions per model on the
paid Console System One API. The direct TypeSafe series above used Jev 1.14.0,
while Console used Jev 1.13.

| Model | Confirmed | Median attempt | Input tokens | Output tokens |
| --- | ---: | ---: | ---: | ---: |
| Jev 1.13 | 58/63 | 4.12 s | 1,370,208 | 110,282 |
| Gemini Decision 1 | 63/63 | 5.04 s | 1,217,843 | 0 |

[Console results by case, token usage and failures](docs/benchmarks/2026-11-17-opencode-console-matrix/report.md)
· [All 126 attempts (CSV)](docs/benchmarks/2026-11-17-opencode-console-matrix/audited-runs.csv).

### Archived measurements

The static charts below keep every model visible at once. Longer workflows and
short tasks have separate figures and cost denominators. Completion comes first;
a fast early stop is not a successful task.

**These measurements predate this release candidate.** They use the former name
**Decision Browser Core** and retain their original revisions, models, failures,
budgets and scoring rules. Their source commits are not part of the new
clean-history repository. These are newly drawn charts of archived data, not new
measurements of this tree.

### Long workflows: 5 tasks, 3 repetitions

![Historical long workflows: confirmed attempts out of 15, median process time and estimated cost for all seven agent/model configurations.](docs/assets/readme-long-workflows.svg)

[Method, per-task outcomes and source revisions](docs/benchmarks/2026-11-03-long-suite/report.md).
Core's 30 attempts were rerun; the other 75 are from the earlier series. The
series ran at different times. The chart uses the latest recorded Core results.

### Short tasks: 16 tasks, 3 repetitions

![Historical short tasks: confirmed attempts out of 48, median process time and estimated cost for all seven agent/model configurations.](docs/assets/readme-short-tasks.svg)

| Source report | What it covers |
| --- | --- |
| [GPT-6 Luna libraries](docs/benchmarks/2026-11-03-decision-browser-article-cost.md) | Library comparison and the context-cost follow-up. |
| [Standalone Terra](docs/benchmarks/2026-11-03-terra-medium/report.md) | The separate Terra comparison and its scoring. |
| [Core with Jev](docs/benchmarks/2026-11-03-main-providers/report.md) | Decision Browser Core provider outcomes, timings and estimated costs. |
| [Other Jev libraries](docs/benchmarks/2026-11-03-browser-providers/report.md) | The jev-ra and jev-ultrafast provider comparison. |

Each configuration's median includes unsuccessful attempts and early stops.
Time includes process/browser startup and cleanup. Costs are estimates for scored
calls, not full experiment spending. The short-suite configurations come from
separate benchmark series. No result here guarantees general-site reliability.
The original [animated comparison](docs/assets/browser-four-agents.gif) remains
available as a historical artifact; the README does not autoplay it.


## Development

Run formatting, Clippy and native tests from the repository root. Chrome tests
need an installed browser and loopback access.

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --all-targets -- --include-ignored --test-threads=1
sh scripts/check-python-free.sh
```

The deterministic suite uses owned fixtures and makes no paid provider requests.
Run paid provider acceptance separately with its recorded model, environment,
cost allowance and output path. [Verification](docs/verification.md)
· [Contributing](CONTRIBUTING.md)

## License

Decision Browser uses the [MIT license](LICENSE). Distributed upstream excerpts
retain their [third-party notices](THIRD_PARTY_NOTICES.md).

# Changelog

Notable user-facing changes are recorded here before tagging a release.
Versions follow the policy in [Releasing](docs/releasing.md).

## [Unreleased]

- Allow ordinary tasks in an explicitly selected Chrome session under
  `task_scope`. Each task uses its own tab and runs without Decision Browser
  forms.

## [0.2.2] - 2026-11-28

- Reuse the confirmed task scope for ordinary actions in isolated Chrome.
  Review actions with unresolved effects and actions in existing Chrome
  individually.
- Let managed Codex installations queue the task and present its confirmation
  directly. The browser starts after the user accepts the task scope.

## [0.2.1] - 2026-11-28

- Resume interrupted Chrome CDP WebSocket upgrades on the same socket within
  the connection deadline. Report errors before provider selection in the
  cancellation test and cover delayed and stalled upgrades with regression tests.
- Publish one npm launcher containing the reviewed Rust executables for macOS
  arm64 and Linux x64. Select and verify the matching bundled executable before
  launch, and document npm setup alongside direct native installation.

## [0.2.0] - 2026-11-28

- Release the Rust runtime from 0.1.5 on the 0.2 compatibility line. Upgrades
  from the Python distribution move to the native executable and a separately
  installed Chrome or Chromium. The six MCP discovery schemas are preserved.
- Document the download targets, managed project upgrades, provider selection,
  credentials and browser scope. Align the CLI help with those commands.

## [0.1.5] - 2026-11-28

- Verify Chrome process-group cleanup without interpreting the state of unrelated
  system processes. Unknown states in the owned group still block confirmation.

## [0.1.4] - 2026-11-28

Tagged release candidate. Its macOS tag verification blocked publication.

- Move the server, project installer, release tools and verification to Rust.
  The native archive runs with Chrome and needs no auxiliary language runtime.
- Run ordinary HTTP(S) browser tasks through CDP in an isolated browser or one
  owned tab of an explicitly selected Chrome. Admit actions at the agreed
  origin, block recognized consequential actions and retain cleanup outcomes.
- Preserve all six MCP discovery schemas, the standard admission form and
  read-only artifact pagination. Add a native `probe-browser` command.
- Handle interrupted native installation and legacy installer boot-witness
  recovery with safeguards for retained files and running clients.
- Build checked native archives, support an optional offline npm launcher, and
  verify the release tree and browser suite in CI.

## [0.1.3] - 2026-11-25

- Forward explicitly configured `UV_SYSTEM_CERTS` through the npm launcher and
  Python installer to uv, including locked package downloads through a configured
  mirror. Preserve custom CA settings and certificate verification. Chromium
  downloads continue to use Playwright's Node client and its CA configuration.
- Report installer failures with the stage, a bounded reason code, a recognized
  public download host or configured-mirror label, and a next action. Keep child
  output, private URLs and CA paths out of diagnostics. Report retained project
  blocks separately and track whether client settings were written.
- Add `install-status` and a recovery preview using an existing Python 3.11+
  interpreter. These commands inspect a blocked project without preparing a
  runtime. Explicit `recover-install --apply` can record signed boot evidence for
  an eligible failed first install, then release the lease after a later boot
  of the same machine and repeated checks of the lease, retained files,
  registrations and process use. Uncertain evidence keeps the block in place.
  Recovery retains runtimes and existing client registrations.

## [0.1.2] - 2026-11-23

- **Jev can choose an action and its target together.** For example, it can
  choose to fill the origin field in one decision. When the page stays the
  same after repeated waits, Jev gets another chance to choose an interaction.
- **Luna can choose a click and its target in one question.** For small sets
  of available actions, Luna gets one question per decision. In ten recorded
  page states, this reduced 33 questions to 10 across the same ten requests.
  The effect on live response time is still unmeasured.
- **Luna sees what a form will submit.** It can see the current dropdown
  selection, which boxes are checked, and what a click would change. This
  helps when a task asks for an option the website marks as optional.
- **Models get clearer scroll information.** Luna sees which links it can click
  and whether scrolling can reveal more content. Jev can see whether its last
  scroll moved the page and whether more content remains in that direction.
- **A temporary OpenAI outage can recover within the same task.** When the
  Decisions API returns HTTP 503, the browser can retry the request once per
  task. The retry counts toward the original time, call and byte limits.
- **The counter setup check shows the model its progress.** After each click,
  the model can see both the current count and the requested count. A separate
  check reads the page to verify the final result.

## [0.1.1] - 2026-11-21

- Install the npm launcher from `@haffa/decision-browser`. The quick start pins
  the version and includes commands for setup, provider configuration and diagnostics.

## [0.1.0] - 2026-11-20

- Run browser tasks from Codex and Claude Code with scope approval, bounded
  execution, independent outcome checks and evidence artifacts.
- Bind browser actions and outcome checks to the observed document. Preserve
  bounded failure and cleanup diagnostics in final task results.
- Report domain failures as MCP tool errors. Support paginated reads of large
  evidence artifacts with per-chunk and full-artifact hashes.
- Record the 17 November browser comparisons across providers with per-attempt
  outcomes and usage. Retain the source snapshots used for those measurements.

- Add a `gemini` preset for Gemini Decision 1 through the OpenCode Console
  System One API, including guided configuration and Codex examples. Use the
  selected OpenCode key and the existing decision budgets without model fallback.

- Prepare pinned, checksum-verified uv automatically in the npm launcher when
  it is missing or too old. Support proxy/custom CA downloads without a shell
  installer. Add npm `configure` and `doctor --json` commands backed by a verified
  cached package runtime; these commands do not reinstall Chromium or edit MCP
  registrations.

- Add guided `setup` for Python, standalone and npm launchers, plus `configure`
  for provider selection and hidden key entry. Save a private user profile on
  macOS/Linux and resolve it consistently in MCP and doctor. Preserve explicit
  environment overrides, isolate credentials across routes, and retain an
  installed runtime when credential entry is cancelled. Provider checks remain
  local-only. Windows users can configure provider credentials through the environment.

- Add `decision-browser install codex|claude|both` and a compact uvx quick start
  using a reviewed local wheel. Standalone installers also accept a positional
  client and default to the current project; legacy options remain supported.

- Add an npm launcher with a bundled, hash-checked installer,
  automatic uv-managed Python 3.12, client selection and current-project defaults.
  Retain native trust/admission, locked dependencies and per-project runtime management.

- Prepare a fresh runtime for every installation attempt, preserving the active MCP on failure.
- Normalize skill manifest paths and serialize non-BMP Codex paths as valid TOML.
- Reject nested source symlinks during fingerprinting and bootstrap assembly.

- Add a checkout-free standalone installer and bundled wheel bootstrap inputs;
  preserve the frozen dependency graph and persistent project launchers.

- Restore the owner-selected MIT license and distribution license metadata.
- Rewrite onboarding around client setup, outcome verification and explicit limits;
  add support guidance and refresh the release-readiness checklist.

- Use bundled full Chromium in headless mode for owned sessions and verification, retaining the sandbox. Add click/screenshot setup probes after reproducing macOS headless-shell timeouts.

- Add a project-scoped installer for Codex and Claude Code, with locked dependencies, sandboxed Chromium checks, conflict detection, explicit upgrades and removal.
- Package linked admission/action documentation and third-party notices; add README and repository metadata.

- Require certifi 2026.10.23 and use uv 0.13.7 in CI to exclude the compromised emSign ECC Root CA - G3 trust anchor.
- Update Playwright to 1.64.1 and Pydantic to 2.14.2, including their required transitive dependencies. Historical benchmark results are not re-measured by this dependency update.

- Prepare the initial Decision Browser distribution, CI and release checks.

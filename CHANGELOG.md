# Changelog

Notable user-facing changes are recorded here before tagging a release.
Versions follow the policy in [Releasing](docs/releasing.md).

## [Unreleased]

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

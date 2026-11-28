# Public release: install, choose a provider, use

Historical design record from the pre-migration implementation. Current native
commands and acceptance requirements are in [verification](verification.md).
The [original source and procedure](https://github.com/HaffASE/decision-browser/blob/7babeb4114130e1bf97bd0f23599405b70965372/docs/public-release-plan.md)
remain available at the pinned revision.


Plan started 2026-11-13. This is a delivery plan, not a public-release claim.

## Product outcome

A user runs one setup command in a project, chooses Codex, Claude Code or both,
chooses a supported provider, enters its key once if needed, and starts browser
tasks from their client. No manual MCP JSON/TOML editing, shell exports, Python
server startup, or endpoint/model selection is needed for a hosted preset.
Local services remain an advanced option and must already be running.

Keep the existing MCP product and bounded browser runner. A standalone desktop
app, new model adapters, unrestricted actions, and automatic publication are
outside this iteration. Initial validation targets macOS and Linux; Windows
support must be earned by a separate installation/credential/browser matrix.

## Findings in the starting tree

| Gap | Evidence | User impact |
| --- | --- | --- |
| Selection and credentials only come from environment variables | `src/decision_browser/provider_config.py:139`, `docs/providers.md:3` | Setup succeeds but the first model task cannot run without more configuration. |
| CLI exposes serve/doctor/install, with no onboarding | `src/decision_browser/cli.py:92` | The user must assemble the final setup from documentation. |
| npm requires preinstalled uv and client CLIs | `npm/bin/decision-browser.cjs:13`, `:108` | The advertised install flow has prerequisite setup steps. |
| Installer already provides an isolated runtime, Chromium smoke test and transactional registration | `scripts/install.py:178`, `:397` | Reuse the existing installer; do not introduce another installation engine. |
| Capability checks distinguish configuration from actual successful execution | `src/decision_browser/server.py:256`, `scripts/verify.py:313` | A stored key alone cannot mean “ready to use”. |
| CI currently exercises Ubuntu/Python, with npm artifact smoke checks | `.github/workflows/verify.yml:18`, `:119` | A public platform promise needs additional clean-machine evidence. |
| Native admission and live-provider acceptance are not established | `tests/native/README.md:3`, `tests/live/README.md:3` | Local fixtures cannot close the most important release gate. |

Line numbers above refer to the inspected starting tree and may move during work.
Existing uncommitted installer fixes are retained and included in verification.

## 1. Guided setup and persistent provider selection — implemented

- Add `decision-browser setup [codex|claude|both]` to the Python CLI, archive and
  npm launcher. Reuse `install`; preserve its noninteractive interface.
- Prompt for the client when omitted; finish provider setup in the installed
  runtime. Reject interactive setup without a terminal before making changes.
- Add `decision-browser configure [provider]` to select/switch a preset and enter
  a key through hidden input. A local route asks only for its existing service
  endpoint/model. No paid API request is made by this command.
- Store one selected profile in the user's configuration directory, outside
  repositories. Use bounded validation, private POSIX permissions and atomic
  replacement. Explain that this is a local credential file, not encrypted
  storage. Do not claim Windows secret-storage support before ACL validation.
- Use the same resolver for MCP production factory, capabilities and doctor.
  Explicit route/endpoint environment overrides bypass the saved profile;
  explicit keys (including empty overrides) retain precedence. Never send a
  stored credential to a different provider/endpoint.
- Give actionable, sanitized errors and make cancelled/failed setup resumable.

Acceptance: a fresh temporary home can configure each preset, restart the
process and resolve the same settings without exported credentials; switching
routes never reuses another route's key; no secret appears in command arguments,
client registration, diagnostics or test reports; existing install/serve/doctor
contracts still work; packed distribution contains the new commands.

Verified 2026-11-13: full verifier passed 2,532 tests plus five subtests; the exact
packed npm artifact completed setup, generated stdio connection and uninstall
in a temporary project. This establishes local onboarding, not live provider or
native admission acceptance.

## 2. One-command public distribution — launcher implemented, publication pending

- Choose and verify registry destinations and package ownership; publish only
  the reviewed artifact. Until then documentation uses local wheel/tarball paths.
- Remove the separate uv installation step via a reviewed, versioned bootstrap
  with platform/architecture checks, integrity verification, proxy/CA support,
  timeouts and actionable download errors. Reuse the pinned Python/runtime flow.
- Make `configure`, doctor and update reachable from the public launcher as well
  as the installed Python runtime; avoid requiring users to discover venv paths.
- Keep client installation/trust explicit; setup must not change the client's
  global account or silently grant tool access.

The npm candidate now bootstraps pinned, checksum-verified uv and exposes
`configure` and `doctor --json`. These commands use a verified package cache,
separate from the project's browser runtime. No new npm dependencies or shell
installer are required. Verification includes the actual packed artifact with
uv absent from PATH; registry ownership/publication and the platform matrix
remain separate work.

Acceptance: on a clean supported machine with the chosen coding client and the
documented package-manager prerequisite, the published command reaches provider
selection without manual Python/uv/config steps; the same path is tested from
the packed artifact outside the checkout.

## 3. Installation and recovery matrix

- Add macOS installation/package CI alongside Linux; define the supported
  versions/architectures from evidence. Validate Windows separately.
- Exercise clean install, repeat, upgrade, interrupted download/setup, stale
  lock, cancelled credential entry, provider switch and uninstall. Include
  paths with spaces, proxies/custom CA and pre-existing unrelated client config.
- Verify secret storage, restrictive permissions and diagnostics on every
  supported OS. Define credential removal independently from project uninstall.

Acceptance: failures leave the previous installation usable, user settings
survive upgrades, and recovery commands work without manual JSON/TOML edits.

## 4. Real first task and provider diagnostics

- Add an explicit bounded provider connection check using synthetic data,
  reporting authentication, quota, timeout, unsupported model and schema errors
  separately. Disclose that a live check can consume provider usage.
- Provide a packaged first-task example with a verifiable result.
- Run actual native acceptance in each supported client: connect MCP, accept or
  decline scope, perform an isolated controlled task, cancel and inspect cleanup.
- Verify every advertised provider with an authorized account/service. Keep
  configuration, network access, native admission and task success distinct.

Acceptance: a new user follows only the quick start and completes a verified
task in each advertised client/provider combination. Store versions, artifact
hashes and results; do not substitute mock results for this evidence.

## 5. Release candidate

- Simplify README around the demonstrated user journey; put environment
  overrides, custom endpoints and development details in advanced guides.
- Run the full verifier on the final tree, inspect wheel/sdist/npm contents,
  align package versions and add dated release notes.
- Close the existing `docs/public-release-checklist.md` gates using fresh
  evidence, then prepare the exact destination/artifacts for publication.

Acceptance: required CI and native/live checks pass for the reviewed candidate;
supported platforms/providers and limitations match evidence; publication is a
separate concrete release action.

## Verification for the first implementation

1. Focused tests for profile persistence, credential separation, CLI interaction,
   cancellation, non-TTY behavior and installer/npm dispatch.
2. Existing provider, doctor, keychain, installer and server regression tests.
3. Ruff, mypy and the complete unit suite in the locked development environment.
4. Build wheel/sdist/archive/npm, install the exact wheel in a temporary runtime,
   and exercise configure/doctor from outside the checkout with a temporary home.
5. Run available owned-browser integration checks; report native/live checks as
   unverified until actually performed.

## Risks and mitigations

- Credentials: private user storage and sanitized boundaries; no implicit
  provider changes or fallback on failures. A future OS credential-store backend
  must preserve these contracts.
- Existing configuration: explicit environment overrides remain supported;
  report when they override the saved profile instead of claiming it is active.
- Partial setup: retain a successfully installed runtime when credential entry
  is cancelled and show a precise resume command.
- External services: preset existence does not guarantee account entitlement,
  quota or API availability; validate these in the live acceptance stage.
- Scope: local onboarding and the npm bootstrap do not establish registry
  publication, native client acceptance or full public-release readiness.

# Local installation for Codex and Claude Code

## Guided setup

On macOS/Linux, with Node.js 20+ and your client CLI on PATH, run the versioned
npm package in an interactive terminal:

```sh
npx --yes @haffa/decision-browser@0.1.2 setup
```

Choose Codex, Claude Code or both. Setup reuses the installer below, then asks
for a provider and a hidden API key (or an existing local service). Append a
client to skip its question. uv and Python are prepared automatically, without
manual installation. `--project` and `--upgrade` work as for `install`.
No provider call is made; after saving the profile, restart the chosen client
and complete its normal trust/MCP prompts.

Provider settings are shared across projects and stored outside repositories in
`~/.config/decision-browser/provider.json` with private permissions. The key is
not encrypted. Use `decision-browser configure [provider]` from the reviewed
package to change it. Environment overrides remain available; see
[precedence and credential storage](providers.md). Existing project overrides
are preserved on upgrade and must be removed to use the saved profile instead.

An explicit absolute `DECISION_BROWSER_CONFIG_FILE` chooses another profile
location. Setup records that nonsecret path in the MCP registration so the
installed client can read the same profile after restarting.

Interactive setup without a terminal fails before installation starts. Cancelling
provider entry keeps the completed installation and prints the exact command to
resume. Use `install` for unattended installation; `setup --uninstall` is invalid.
Windows profile-key persistence is not supported yet; use environment settings.
The client and Linux host-library prerequisites below still apply. If you already
use uv, the Python-wheel alternative is
`uvx --python 3.12 --from /path/to/decision_browser-0.1.2-py3-none-any.whl decision-browser setup`.

## Unattended install

The CLI follows [jev-ra's install flow](https://github.com/brnyxx/jev-ra#quick-start):
choose a client in one command, then let that client start MCP. Decision Browser
keeps its existing project-scoped, locked installer rather than saving a temporary
uvx/npm cache path in client settings.

With uv 0.13.7+ and your client CLI on `PATH`, run from the project directory:

```sh
uvx --python 3.12 --from /path/to/decision_browser-0.1.2-py3-none-any.whl decision-browser install codex
```

Use a reviewed local wheel from the [build procedure](releasing.md). Replace
`codex` with `claude` or `both`. uv can manage Python 3.12 automatically. The
outer uvx environment loads the reviewed wheel and its declared dependencies;
project runtime setup then uses the bundled frozen lockfile. No provider key is
needed until the client runs a model-backed task.

Already installed the reviewed package? Use `decision-browser install codex`.
`--project "/path/to/project"` changes the default current directory, and
`--upgrade` / `--uninstall` retain the behavior documented below. The CLI's
`install --help` does not start setup, though an outer uvx invocation may first
resolve its Python/package environment.

The Python commands above install the wheel attached to the GitHub release.

## npm launcher (no manual uv or Python)

With Node.js 20+, npm/npx and your client CLI on PATH, run this
from the project you want to configure:

```sh
npx --yes @haffa/decision-browser@0.1.2 install both
```

Use `decision-browser setup` instead of `decision-browser install both` to choose
the client and provider interactively in the same flow.

To use the downloaded release tarball instead of the registry, run:

```sh
npx --offline --yes --package=/absolute/path/haffa-decision-browser-0.1.2.tgz decision-browser install both
```

Choose `codex`, `claude` or `both`. The default project is the current directory;
`--project "/absolute/path with spaces"` selects another existing project.
`--upgrade` and `--uninstall` forward to the same installer described below.
The npm removal path also prepares uv and managed Python when needed;
the direct `.pyz --uninstall` route needs only an existing suitable Python.

The launcher verifies its bundled `.pyz` against its SHA-256 manifest, then runs
it using uv-managed Python 3.12. There is no lookup of the Decision Browser
package on PyPI, dependency on a source checkout, global npm installation,
background server to start manually, or shell interpolation. MCP registrations
still point to the persistent per-project environment, not an npm cache path.
The package hash checks consistency, not publisher identity; obtain the candidate
from a trusted maintainer. Provider API keys are not forwarded to setup. Explicit installer index, proxy
and CA settings are forwarded. The interactive provider step reads/saves the
private user profile; inherited model keys remain excluded from npm bootstrap children.

If uv 0.13.7+ is on PATH, the launcher reuses it. Otherwise it downloads pinned
uv 0.13.7 from Astral's official release service, checks the archive's pinned
SHA-256, extracts only the executable and checks its version before use. There
is no shell installer or global PATH change. The bootstrap supports arm64/x64
macOS and Windows and arm64/x64 Linux using Astral's static musl builds. This
asset table does not by itself establish native platform acceptance.

The launcher does not install client CLIs. Python may be downloaded by uv;
dependencies and Chromium are also downloaded. `npx --offline` applies only to
npm, preventing fallback to an unrelated registry package. It is not an offline
browser/dependency bundle. Setup never invokes sudo or relaxes browser security.

macOS, Linux and Windows are targeted through Node/uv without shell scripts.
Linux needs supported Playwright host libraries and Chromium sandbox support.
Each OS still requires native end-to-end validation; mocked launcher tests do not
prove that first-run downloads or client registration work on that OS. CI builds
and packs the candidate on Linux without publishing it. Trust and MCP approval,
the installed `standard_form` admission mode, and conflict/rollback behavior are
unchanged.

### Configure and diagnose from npm

Use the same reviewed artifact, without finding a Python environment manually:

```sh
npx --yes @haffa/decision-browser@0.1.2 configure
npx --yes @haffa/decision-browser@0.1.2 doctor --json
```

`configure [provider]` needs a terminal and changes the shared user profile.
`--project` selects the working directory; it does not create a project-specific
profile. `doctor --project /path/to/project` inspects the chosen installation.
Doctor reports local configuration and installation observations, without an API
request. Preparation diagnostics go to stderr and doctor JSON goes to stdout.

Both commands prepare a private package runtime from the bundled wheel and frozen
lockfile on first use, then verify and reuse it. They do not download Chromium,
edit MCP registrations or execute commands found in project configuration.
Supported provider environment overrides reach only the final command process,
after preparation. A failed preparation leaves the project installation intact.

The launcher cache defaults to `~/Library/Caches/decision-browser` on macOS,
`${XDG_CACHE_HOME:-~/.cache}/decision-browser` on Linux, and
`%LOCALAPPDATA%/decision-browser` on Windows. Set an absolute
`DECISION_BROWSER_CACHE_DIR` to change it. The cache holds versioned uv binaries
and command runtimes; uv manages its downloaded Python cache separately. Keep
the launcher cache private and at a nonsymlinked path. Changed cached binaries
or runtime sources are rejected. After an integrity error, remove only the
affected cache version and retry; project registrations and the provider profile
are stored separately. After an interrupted preparation, ensure its process has
ended before removing its `prepare.lock` and retrying.

The download design follows Astral's [installation documentation](https://docs.astral.sh/uv/getting-started/installation/)
and [platform policy](https://docs.astral.sh/uv/reference/policies/platforms/).

## Standalone archive (alternative)

Prerequisites: Python 3.11+ (3.12 recommended; CI covers 3.11–3.13), `uv` 0.13.7+, the
selected client CLI(s) on PATH, a writable existing project, and network access to
PyPI and Playwright's browser CDN. Install clients using their official instructions.
The installer does not install Python, uv or the clients. If `python3` resolves to
an older Python, use `python3.12` explicitly. Missing Python is a shell error before
the installer can run. On Linux, install Playwright's OS dependencies separately
with administrator approval; the installer never invokes sudo or disables sandboxing.

```sh
python3.12 /path/to/decision-browser.pyz codex
```

Add `--setup` for provider onboarding; omit `codex` with that flag to choose the
client interactively.

Run from the target project; choose `codex`, `claude` or `both`. Paths with spaces
are supported; quote them. `--project` optionally selects another existing project.
The legacy `--client codex` syntax remains supported. No global client settings are edited.
Both means both CLIs must be present before installation starts. Current local
validation uses Codex CLI 0.166.0 and Claude Code 2.1.314; this is not a claim
that every earlier version supports these settings or elicitation.

The installer uses `uv sync --frozen --no-dev --no-install-project` by default.
Release archives carry the reviewed wheel; the installed-package route verifies
its RECORD hashes and repacks it without a build backend. Both install that wheel
without resolving dependencies. Only a source checkout builds a wheel with pinned
build constraints. It copies the package into `.decision-browser-install/runtimes/<source-id>-<attempt-id>/venv`,
downloads full Chromium (without the unused headless shell) into that runtime, launches the bundled full Chromium with `channel="chromium"` and
`chromium_sandbox=True`, checks native click and PNG screenshot on an in-memory page, and checks MCP server construction. No provider call
or credentials are required for these setup checks. Network/build child output is
suppressed to avoid disclosing credentials in failures; a failed stage is named.

Project files:

| Client | stdio registration | Skill |
| --- | --- | --- |
| Codex | `.codex/config.toml`, `mcp_servers.decision_browser` | `.agents/skills/decision-browser` |
| Claude Code | `.mcp.json`, `mcpServers.decision_browser` | `.claude/skills/decision-browser` |

Both registrations invoke the installed Python with `-I` and explicit
`--admission standard_form`. Existing settings and other servers are preserved;
Claude's JSON formatting may be normalized. Existing same-name servers or skill
directories are conflicts, even if apparently equivalent. Edits to installer-owned command, arguments and browser paths remain conflicts.
Named nonsecret provider environment settings and supported `env_vars` names
are preserved on upgrade, including an empty or omitted `env_vars` list;
literal API keys and unknown environment entries remain
conflicts. Provider preferences are not copied into the installer manifest. Symlinked target configuration paths are rejected.
Keep the runtime out of version control. Generated registrations contain absolute
machine paths and should normally stay local. No ignore rules are written to your
project automatically. Do not move the target project after setup; uninstall first.

`serve --admission ...` is the explicit mode policy and does not read project
configuration. The legacy implicit fallback reads only a bounded, regular,
user-owned `.codex/config.toml` and directory without symlinks or group/world
write permission on POSIX. It recognizes only the exact current executable,
project directory and Keychain launcher arguments. Missing or unrelated
configuration selects `standard_form`; an unreadable, malformed or oversized
file stops startup with a fixed `project_policy_unreadable` or
`project_policy_invalid` reason. The source (`explicit_cli`, `project_launcher`
or `default_standard_form`) is written to stderr as `admission_policy_source`;
MCP stdout remains reserved for protocol messages. This does not replace the
client's project trust decision.

## Restricted networks and diagnostics

If uv reports `invalid peer certificate: UnknownIssuer`, confirm with your
administrator that the issuing CA is trusted by the operating system. With a
launcher built from this checkout, opt in to the system trust store for uv in
a fresh project. For example, from that project on macOS or Linux, run

```sh
UV_SYSTEM_CERTS=true npx --offline --yes --package=/absolute/path/to/reviewed-launcher.tgz -- decision-browser setup codex
```

Use the path to the tarball containing the fix. The published 0.1.2 launcher
filters out this variable. Leaving it unset keeps uv’s default trust settings.
Explicit values reach uv for managed Python and locked packages, including
downloads through a configured mirror. TLS certificate verification stays enabled.

For a reviewed HTTPS package mirror, set `DECISION_BROWSER_INSTALL_INDEX_URL`
in the environment of the installer. For example:

```sh
DECISION_BROWSER_INSTALL_INDEX_URL=https://mirror.example.org/simple python3.12 /path/to/decision-browser.pyz codex
```

The mirror must serve the locked versions and artifacts. The mirror route exports
requirements from the frozen lock and installs with `--require-hashes`; it does
not rewrite the lock or resolve newer versions. URLs with embedded credentials,
query strings or fragments are rejected. The outer `uvx` bootstrap has its own
network settings; use uv's index configuration for that outer command too, or use
the standalone archive to avoid an extra package-resolution stage.

Setup forwards `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY` (and lowercase
variants), `SSL_CERT_FILE`, `SSL_CERT_DIR` and `NODE_EXTRA_CA_CERTS`.
Playwright downloads Chromium with its own Node client. For that stage, set `NODE_EXTRA_CA_CERTS`
to the PEM certificate file, as described in the
[Playwright proxy instructions](https://playwright.dev/python/docs/browsers#install-behind-a-firewall-or-a-proxy).
Provider credentials are excluded.
Chromium uses its own download service, so a PyPI mirror alone does not make setup
offline. Proxy/CA settings must also permit that download.

The npm uv download honors `HTTPS_PROXY` or `ALL_PROXY` (including lowercase
variants), `NO_PROXY`, `SSL_CERT_FILE` and `NODE_EXTRA_CA_CERTS`. An `HTTP_PROXY`
alone does not route this HTTPS download; use `HTTPS_PROXY`. TLS verification is
always enabled. The first bootstrap also needs access to
`releases.astral.sh` and its release-download redirects. An index mirror does
not replace this uv download or uv's managed Python download.

Setup prints the selected uv path/version. If it reports an older uv after an
update, fix PATH order in the shell launching the installer. Each project
runtime preparation stage emits a JSON diagnostic on stderr. Read `stage` and
`code` for the operation and its outcome. On failure, `hint` identifies the
cause when known, `target` identifies the download service when known, and
`next_action` suggests what to check. For example,
`tls_unknown_issuer` on `locked Python package` points to uv’s certificate
check. A public download host may be named. The installer labels a private
index as “configured mirror” and suppresses child logs.

If setup reports `cleanup: unknown_retained`, fix the underlying CA or proxy
problem and inspect the block separately. An idle lease file is normal after
a successful install. Keep the lease, residual and runtimes in place. See
[inspecting and recovering a blocked first install](#inspect-and-recover-a-blocked-first-install).

The doctor also reports registration, runtime-file and skill presence separately
for Codex/Claude. These are on-disk observations, not proof of client trust,
connection or provider entitlement; its next steps identify those remaining checks.

### Inspect and recover a blocked first install

Use a tarball built from this change. The published 0.1.2 launcher predates
these commands. They can inspect an affected 0.1.2 project using an existing
Python 3.11+ interpreter, including a locally installed uv-managed Python.
They do not prepare a command runtime, install dependencies or start Chromium.

```sh
package=/absolute/path/to/reviewed-launcher.tgz
project=/absolute/path/to/affected-project
npm exec --offline --yes --package="$package" -- decision-browser install-status --project "$project" --json
```

`install-status` and `recover-install` without `--apply` are read-only.
The JSON fields `status`, `reason`, and `recovery` describe the block and the
available action. A blocked result exits with code 1, including when the
command successfully records evidence and the block remains in effect.

A 0.1.2 failure receipt records a PID but cannot identify detached descendants
or the boot in which the failure happened. A disappeared PID or an old file
is insufficient to release the lease. For an eligible failed first install
on a supported local filesystem, use the following sequence:

1. Close clients and processes using the retained installation. When the
   report shows `recovery=record_reboot_witness`, explicitly record the
   current boot and project evidence. Run
   `npm exec --offline --yes --package="$package" -- decision-browser recover-install --project "$project" --apply --json`.
   The block stays in place. On first use, this creates a private signing
   key under the account’s home at `.local/state/decision-browser/recovery/key`.
   Keep that key and the project’s recovery record through the reboot.
   A missing key or an edited record leaves the block in place.
2. After the next full restart of the **same machine**, inspect the project
   again. Restarting a terminal or a coding client does not change the boot.
3. If the report shows `recovery=ready`, issue the same `--apply` command.
   It repeats the checks under the installation lease. Success reports
   `status=recovered`. It archives the residual and returns the existing
   lease inode to idle. The failed runtime remains on disk and existing
   registration files are preserved. You can now run setup with the corrected
   network settings.

Recovery targets macOS and Linux projects on supported local volumes. The
legacy receipt cannot identify a single failed runtime, so an existing
installation manifest or a local MCP launcher with unknown dependencies
keeps the block in place. Unrelated remote HTTP MCP registrations can stay.
An active installer, changed files, a busy runtime or incomplete process
visibility also prevents release. Container and restricted inspection
environments may report that recovery is unavailable. Current process checks
are observations under the installer lease. Keep clients closed throughout
recovery. Use a fresh project to test corrected TLS settings while a block
remains. The commands never signal a recorded PID.

#### Reproduce locally with a private mirror

Use a disposable local **persistent** directory if you plan to test across a
reboot. From the PR checkout, with Node.js 20+, OpenSSL, Python 3.11+, uv
0.13.7+ and the Codex CLI available, build a local launcher and run:

```sh
artifacts=$(mktemp -d "$HOME/decision-browser-artifacts.XXXXXX")
uv build --wheel --no-sources --build-constraints scripts/build-constraints.txt --out-dir "$artifacts"
python3.12 -I scripts/build_npm.py --wheel "$artifacts"/*.whl --output "$artifacts/package"
(cd "$artifacts/package" && npm pack --offline --ignore-scripts --pack-destination "$artifacts")
python3.12 -I tests/native/installer_recovery_smoke.py --package "$artifacts"/*.tgz --work-dir "$HOME/decision-browser-recovery-test"
```

Choose a new work directory. The fixture runs the packed `install codex`
with `UV_SYSTEM_CERTS=true` and a loopback HTTPS mirror signed by an
untrusted test CA. This exercises the same locked dependency stage as
`setup codex` without a provider prompt. It leaves system trust intact,
checks the read-only commands, records the boot witness and verifies that
same-boot recovery stays blocked. To generate the failure with the original
0.1.2 installer, also pass `--installer-package /path/to/0.1.2.tgz`.
Keep the reviewed tarball somewhere persistent and use the printed project
path for the post-reboot steps above.

## Credentials and first real use

Restart clients in the target project. Codex loads project configuration only
for trusted projects. Claude Code has its own project MCP approval. The installer
does not mark a project trusted, pre-approve tools, or alter client policy.

The saved provider profile is read automatically. Advanced alternatives are the
client's inherited environment or an existing secret manager's launch integration.
Codex forwards only named provider environment variables; Claude inherits the
launch environment. API keys are stored only in the private user profile when
entered through `configure`, never in MCP config, arguments or the installer
manifest. No Keychain lookup or API call is performed by installation. See
[providers](providers.md).

Open the client's MCP listing and confirm `decision_browser` connects. Ask for the
`decision-browser` skill and call `capabilities`. A real ordinary browser task
requires the client to present the pending form and the user to respond. Never
fabricate `reviewed: true`, bypass admission, or treat a mock callback as a human.
Prefer the npm `doctor` command above. When invoking the skill's doctor directly,
use the installed runtime Python, not an unrelated system Python; locate that
interpreter in the generated MCP registration.

Local package/browser READY is distinct from provider configuration, API entitlement,
client MCP loading, native form approval and end-to-end task PASS. Installation
reports these latter checks as NOT_RUN. Elicitation support is documented by both
clients but depends on the actual client version/runtime and interactive session.

## Repeat, update and remove

Repeating the command prepares and probes a fresh runtime before switching managed
registrations. The active runtime is never synchronized or rebuilt in place. It does not download an unpinned latest Decision Browser. Review an
updated distribution and use the explicit upgrade option for all installed clients:

```sh
python3.12 /path/to/decision-browser.pyz --client both --project "/absolute/path/to/project" --upgrade
python3.12 /path/to/decision-browser.pyz --client both --project "/absolute/path/to/project" --uninstall
```

Every install attempt uses a unique permanent runtime path, including repeats and
upgrades. A failed build or interrupted preparation leaves the active runtime and
registrations intact. Unreferenced partial runtimes may remain after interruption.
Old runtimes remain for diagnosis; there is no automatic update or garbage collection.
Uninstall removes only unchanged managed server entries and skill files; other
configuration remains. It retains runtimes/cache under `.decision-browser-install`.
After all clients are uninstalled, you may remove that directory yourself.
The wheel includes `decision-browser-install` and its lockfile/build inputs. No
`--source` is required; that option remains a developer override. The standalone
archive extracts the same inputs into a private temporary directory, removes them
after setup, and leaves registrations pointing only at the persistent project runtime.

Download/browser failures happen before registration changes. A failed,
timed-out or interrupted owned stage retains its lease and runtime. Follow
the [blocked-project guidance](#restricted-networks-and-diagnostics).
Caught write failures roll back files changed by that attempt, unless another writer
has since modified them. Close clients while changing registrations. Multi-file writes
are not crash-atomic: after power loss or forced termination, review partial entries
and the manifest; the next run fails closed on conflicts. Project state is read,
planned and committed under `.decision-browser-install.lease`, a persistent private
kernel-lock inode. The kernel releases its lock on process exit/crash; the file is
not deleted during release. An idle crash is recoverable, while a durable ACTIVE
stage intent refuses another preparation even after the installer root dies.
Replaced, symlinked, hardlinked or foreign lease files
are rejected. Unknown legacy `install.lock` or `prepare.lock` files remain a
conservative refusal; their age is not evidence that deleting them is safe.
No recovery silently overwrites an unowned entry. No global settings backup containing
secrets is created. Empty parent directories may remain after rollback/removal.

## Official configuration references (checked 2026-11-05)

- [Codex stdio MCP, project scope and environment forwarding](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex repository skills](https://learn.chatgpt.com/docs/build-skills)
- [Codex app-server elicitation](https://developers.openai.com/codex/app-server)
- [Claude Code stdio MCP, scope, approval and elicitation](https://code.claude.com/docs/en/mcp)
- [Claude Code project skills](https://code.claude.com/docs/en/skills)

A future public release may offer a version-pinned package command after the
owner approves publication and verifies an actual release.
There is intentionally no speculative public install command here.

## Browser channel compatibility

Owned isolated sessions and verification fixtures use Playwright's `chromium`
channel (the bundled full Chromium in headless mode), not its separate default
headless shell. Sandbox, action/admission policy and test deadlines are unchanged.
The first macOS verification with Playwright 1.64.1 reproduced native click and
screenshot timeouts in a standalone headless-shell probe with no Decision Browser
code; the same probe passed using full Chromium 156.0.8078.4. Setup now tests both
operations rather than only opening a page. Do not use `--only-shell` installation.
This changes the browser binary selection for future runs, not historical benchmark
results or the provenance of archived reports.

[Official Playwright browser/channel documentation](https://playwright.dev/python/docs/browsers#chromium-new-headless-mode)

## Building and distributing the installer

See [release builds](releasing.md). The `.pyz` starts using Python's standard library,
so it needs no temporary uvx environment or global package installation. It still
requires Python, uv and the chosen client CLIs. Download `decision-browser.pyz` and `SHA256SUMS` from the
[versioned GitHub release](https://github.com/HaffASE/decision-browser/releases/tag/v0.1.2)
and verify the archive checksum before execution.

For development, from a checkout use:

```sh
uv run --no-project --python 3.12 python scripts/install.py codex --project /path/to/project
```

This uses the standard-library bootstrap without resolving the development project.
With Python already available, `python3.12 scripts/install.py codex` installs into
the current directory. The legacy `--client` syntax still works. All routes use the
same installer and frozen project lock.

Skill manifest paths use forward slashes on every platform. Native Windows installation
has not been exercised; Windows path normalization is covered by portable regression tests.
Source symlinks (including nested directories) are rejected during fingerprinting and wheel bootstrap assembly.

## Preparation transactions and bounded failures

The command-runtime cache uses the same kernel lease around owner initialization,
receipt reads and preparation. Missing managed registrations and skill files can
be recreated by a reviewed install, or left absent by uninstall. Modified files,
unrecorded files and unrelated settings still cause a conflict.

Installation metadata is size-bounded, rejects duplicate keys and validates its
schema before planning. Writes fsync file contents before replacement and, on
POSIX, the held parent directory afterwards. POSIX replacements use no-follow
directory descriptors plus identity checks. These checks reduce pathname races;
they are not a multi-file crash transaction or protection against every writer
already authorized to rename the project. Windows keeps bounded pathname identity
checks and file fsync; native Windows filesystem behavior is not qualified here.

Each POSIX preparation stage starts a new session with child-only umask `077`.
A stage timeout signals only that owned process group, escalates to SIGKILL, reaps
the direct child and checks for runnable group members. This does not prove that
detached browser processes stopped: Playwright launches Chromium in a separate
POSIX group. Timeout, failure and interruption therefore retain uncertainty even
when the original group has disappeared.
Zombie nonchildren are not described as reaped. Deliberately escaping sessions are
outside this process-group contract. Child output is drained into a bounded tail;
only fixed hints such as `tls_certificate`, `disk_space` and `permissions` are
reported, never raw credentials, endpoints or child logs.

Before process launch, the held lease receives a complete ACTIVE intent, fsync and
readback; short writes or failed persistence abort launch. Only a successfully
completed stage with observed original-group/output quiescence returns it idle.
Private standalone stage calls acquire the same lifecycle lease automatically.

On minimal Linux images, install `procps` to provide `ps` for inspecting remaining
process-group members. When `ps` is absent, the installer can confirm an already
vanished group with a POSIX process-group probe. A group that still exists or
cannot be probed retains the blocked lease and reports `install_procps`.

If stage/group/output completion is uncertain, preparation retains a blocked lease and
`.decision-browser-stage-cleanup-unknown.json`; another install/prepare refuses to
start. ACTIVE or blocked lease state is authoritative for retaining the runtime,
even if the JSON or a later poison write fails. No age/PID guess reclaims it.
The receipt's PID is diagnostic data, never authority to kill a later
process with that PID. On Windows a timeout makes a PID-scoped `taskkill /T /F`
attempt and retains this uncertainty even when that command succeeds. A root wait,
console process group or portable unit test is not native Windows tree proof.

Bootstrap archive guards reject drive/colon/ADS names on every platform. The uv
ZIP extractor checks CRC consistency in addition to its existing locked full-archive
SHA-256 and version checks. This preserves the trusted-bundle boundary; CRC is not
a substitute for source authentication.

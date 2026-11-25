# Decision Browser launcher

Install the MCP server, project skill and Chromium for Codex or Claude Code,
without running Python commands or managing a server yourself. The coding client
starts and stops the server through MCP.

Prerequisites: Node.js 20+ (including npm/npx), an existing project, and the
selected client CLI(s) on PATH. The launcher prepares uv automatically when a
suitable version is absent; Python 3.12 is managed by uv. It does not install
the coding clients. Downloads require
network access; Linux also needs Playwright's host libraries and a working
Chromium sandbox. No sudo or security-setting changes are performed.

Start guided setup from your project:

```sh
npx --yes @haffa/decision-browser@0.1.3 setup codex
```

Use `setup claude` or `setup both` for the other client choices. To install from the
release tarball you downloaded, run:

```sh
npx --offline --yes --package="/absolute/path/haffa-decision-browser-0.1.3.tgz" decision-browser setup
```

Plain `setup` prompts for the client. Select a provider and enter its key through
hidden input for a hosted preset. On macOS/Linux the profile stores the key in
plaintext outside projects with private permissions. On Windows use environment
settings for credentials. Provider selection does not call the API. Cancelled
key entry retains the installation and prints an exact resume command.

For unattended installation without provider prompts:

```sh
npx --yes @haffa/decision-browser@0.1.3 install codex
npx --yes @haffa/decision-browser@0.1.3 install claude
npx --yes @haffa/decision-browser@0.1.3 install both
```

`--offline` prevents npm from substituting a registry package. It does **not**
make runtime setup offline: uv/Python, locked package and Chromium downloads
still need their official services. To select another project, append
`--project "/path/with spaces"`. Repeat the command to check an installation;
append `--upgrade` for a reviewed new candidate, including all installed clients.
Append `--uninstall` to remove unchanged managed registrations and skills.
Runtime/cache files are retained for manual cleanup.
The npm removal command also prepares uv and managed Python when needed. The
direct Python `.pyz --uninstall` route can remove
registrations without uv when a suitable Python is already available.

Change your provider or inspect the current project using the same package:

```sh
npx --yes @haffa/decision-browser@0.1.3 configure
npx --yes @haffa/decision-browser@0.1.3 doctor --json
```

`configure [provider]` needs an interactive terminal. `doctor --project /path`
checks another project. Both use a private cached Python package runtime without
installing Chromium or changing client registrations. The first call can download
Python and locked dependencies; later calls verify and reuse that runtime.
Preparation messages go to stderr, so `doctor --json` keeps stdout as JSON.
Doctor checks local configuration; it does not test provider access or client trust.

Use `configure gemini` for Gemini Decision 1 through OpenCode Console. This
preset uses an OpenCode Console API key and selects `gemini-decision-1`.

The uv bootstrap downloads version 0.13.7 from Astral's official releases and
checks its pinned SHA-256 before extracting or executing it. A uv 0.13.7+ already
on PATH is reused. Downloads support `HTTPS_PROXY` / `ALL_PROXY`, `NO_PROXY`,
`SSL_CERT_FILE` and `NODE_EXTRA_CA_CERTS`; TLS verification remains enabled.
No shell profile or global PATH changes are made. Tool and command-runtime caches
default to `~/Library/Caches/decision-browser` on macOS,
`${XDG_CACHE_HOME:-~/.cache}/decision-browser` on Linux, and
`%LOCALAPPDATA%/decision-browser` on Windows. An absolute
`DECISION_BROWSER_CACHE_DIR` overrides this location. uv manages its Python cache
separately. A modified tool/runtime cache is rejected rather than executed.

For Python downloads behind a corporate proxy, set `UV_SYSTEM_CERTS=true`
when the issuing CA is trusted by the operating system. This launcher forwards
the explicit value to uv for both the frozen lockfile and a configured mirror.
For the Chromium download, Playwright’s Node client accepts an additional CA
bundle via `NODE_EXTRA_CA_CERTS`. If setup reports a blocked project, keep the
lease and runtime in place and use a fresh project to test the corrected settings.

Inspect an installation block using an existing Python 3.11+ interpreter:

```sh
npx --yes @haffa/decision-browser@0.1.3 install-status --project /path/to/project --json
npx --yes @haffa/decision-browser@0.1.3 recover-install --project /path/to/project --json
```

These commands inspect local state without preparing a runtime. `recover-install`
previews recovery. The explicit `--apply` option records boot evidence or completes
recovery after verification on a later boot of the same machine. Uncertain
process or file state keeps the block in place. See the
[blocked-install procedure](https://github.com/HaffASE/decision-browser/blob/main/docs/installation.md#inspect-and-recover-a-blocked-first-install).

The package contains a checked `.pyz` installer and SHA-256 manifest. It never
looks up a Python package by name on PyPI, and has no npm runtime dependencies or
lifecycle hooks. Runtime Python dependencies come from the embedded lockfile.
MCP settings point to a persistent project runtime, never to the npm/uv cache.
The hash detects a broken package; it is not publisher authentication.

Unattended installation does not require a model key. Inherited model credentials
and unrelated Python/uv environment overrides are not passed to bootstrap children. Only
the prepared configure/doctor process receives supported provider overrides. Guided
setup asks for credentials only after installation; the MCP server then reads
the saved profile automatically. Advanced environment configuration remains
available. Restart your client in the project and review its trust/MCP/admission
prompts. Those approvals are unchanged.

The launcher uses portable Node/uv APIs for macOS, Linux and Windows, but each
platform still needs native installation verification before a release claims
support. See the repository's [installation guide](https://github.com/HaffASE/decision-browser/blob/main/docs/installation.md).

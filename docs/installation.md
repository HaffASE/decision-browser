# Installation

Install a reviewed native archive matching the operating system and CPU. Chrome
or Chromium and the coding client are separate prerequisites. The archive
contains a Rust executable, license notices, manifest and SHA-256 checksums.
See [native build and archive verification](native-installation.md).

## Register a project

Extract the archive after checking its outer hash, then check the enclosed
`SHA256SUMS`. Run its executable with the selected project.

```sh
/absolute/path/to/bundle/bin/decision-browser setup codex --project /absolute/path/project
```

Choose `claude` or `both` for the client. Interactive setup installs first and
then configures the provider. For unattended registration, use `install codex`.
The native installer copies its executable into a persistent project runtime,
then writes managed client configuration and the skill. Restart the coding
client and review its project trust and MCP prompts.

The archive can install and run on a prepared machine without Python, uv,
Node.js or package downloads. Model-driven tasks still need network access and
credentials for their selected provider. Source builds require Rust and Cargo.

```sh
cargo build --locked --release --bin decision-browser
./target/release/decision-browser install both --project /absolute/path/project
```

## Configure a provider

```sh
decision-browser configure
decision-browser doctor --json --project /absolute/path/project
```

On supported Unix systems, the private provider profile is stored outside the
project at `~/.config/decision-browser/provider.json`. Named environment settings
and macOS Keychain services can also provide the selected route's credentials.
Saving a key establishes configuration prerequisites. Use a bounded browser task
to test actual provider access. See [providers](providers.md).

## Update or remove

Use the reviewed replacement executable, and name the installed clients.

```sh
/absolute/path/new/bundle/bin/decision-browser upgrade both --project /absolute/path/project
/absolute/path/bundle/bin/decision-browser uninstall both --project /absolute/path/project
```

The installer verifies managed ownership and stops if client settings or a
managed skill have changed unexpectedly. Other settings are preserved. Runtime
versions may remain retained for active clients and recovery. Close clients
before changing their registrations.

## Inspect an interrupted installation

```sh
decision-browser install-status --json --project /absolute/path/project
decision-browser recover-install --project /absolute/path/project
```

Read the recovery preview and follow its reported state. Eligible recovery is
explicit.

```sh
decision-browser recover-install --apply --project /absolute/path/project
```

Recovery checks the retained project state before changing owned files. An
active installer, edited registration, uncertain resource ownership or
inconsistent boot evidence keeps the project blocked. The [native recovery
procedure](native-installation.md) documents current preview and apply behavior.

## Existing Chrome

Use `existing_chrome` only when `capabilities` advertises it and a selected
Chrome debugging session is available. `DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT`
can select its absolute `DevToolsActivePort` file. The path, ownership and
loopback endpoint are checked before attachment. Chrome must already have
remote debugging enabled with the selected profile.

The task creates and owns an acknowledged tab in that session. Existing account
cookies are shared. The task's exact-origin scope can prevent cross-origin
navigation, form submissions and externally hosted resources. Use a dedicated
test profile for initial acceptance. A test that preserves a foreign tab in a
disposable browser establishes that tested cleanup path.

## Historical distributions

Version 0.1.3 and earlier shipped a Python runtime and its installer. Those
released artifacts and their original instructions remain available in
[the v0.1.3 source](https://github.com/HaffASE/decision-browser/tree/v0.1.3).

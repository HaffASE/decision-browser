# Installation

Version 0.2.0 starts the native Rust compatibility line. Published archives are
available for macOS arm64 and Linux x64. Choose the matching archive from the
[v0.2.0 release](https://github.com/HaffASE/decision-browser/releases/tag/v0.2.0).

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

Choose `claude` or `both` for the client. Bare `setup` asks for the client in a
terminal. `setup codex` and `install codex` register that client directly.
Configure the provider in a separate command after installation.
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

In the commands below, use the extracted executable's absolute path in place
of `decision-browser`, or add its `bin` directory to your executable search path.

```sh
decision-browser configure openai
decision-browser doctor --json --project /absolute/path/project
```

Choose `typesafe`, `gemini`, `openrouter`, `opencode` or `local` for another
preset. `configure` saves the selected preset in the private profile at
`~/.config/decision-browser/provider.json`. Supply credentials through the
selected route's environment or macOS Keychain settings before the client
starts the server. Read `doctor.provider` for actual prerequisites and use a
bounded browser task to test provider access. See [providers](providers.md).

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

For a managed installation from 0.1.3 or earlier, close the coding client and
run the new native executable's `upgrade` command for the installed clients.
The installer checks the previous ownership record before replacing its managed
registration. Resolve a reported configuration conflict or retained recovery
barrier before trying the upgrade again. Custom launch commands need to point
to the native executable with `args = ["serve", "--admission", "standard_form"]`.

Versions 0.1.4 and 0.1.5 introduced the Rust cutover. Version 0.2.0 assigns that
incompatible installation and runtime change a minor-version boundary.
The 0.1.3 Python distributions and their original procedures remain available
in [the v0.1.3 source](https://github.com/HaffASE/decision-browser/tree/v0.1.3).

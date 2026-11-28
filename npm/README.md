# Decision Browser native npm launcher

The public `@haffa/decision-browser` package contains the Rust executables for
macOS arm64 and Linux x64. The launcher selects the matching executable and
checks its SHA-256 before running it. Node.js 20 or newer is required.

Install Chrome or Chromium separately, then run from your project.

```sh
npm exec --yes --package=@haffa/decision-browser@0.2.2 -- decision-browser setup codex
npm exec --yes --package=@haffa/decision-browser@0.2.2 -- decision-browser configure openai
```

Choose `claude` or `both` for the other client registrations. `setup` copies the
native executable into the project's persistent runtime and writes managed MCP
settings. Restart the coding client, review its trust and MCP prompts, and
supply credentials through the [provider guide](https://github.com/HaffASE/decision-browser/blob/v0.2.1/docs/providers.md).

The package has no lifecycle hooks or npm dependencies. Installation and launch
use the bundled executable and local files. Set `DECISION_BROWSER_CHROME` to the
browser executable when automatic discovery is insufficient.

For direct native installation, use the [v0.2.1 release archives](https://github.com/HaffASE/decision-browser/releases/tag/v0.2.1).
Source builds and release packaging use the [release procedure](https://github.com/HaffASE/decision-browser/blob/v0.2.1/docs/releasing.md).

Maintainers can test a reviewed local tarball in a fresh directory.

```sh
npm exec --offline --yes --package=/absolute/path/haffa-decision-browser-0.2.2.tgz -- decision-browser --version
```

The checked-in package template stays private. The release builder creates the
public package after verifying both platform candidates.

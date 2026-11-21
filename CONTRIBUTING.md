# Contributing

Decision Browser is an experimental Python MCP server. Start with the
[README](README.md), [provider guide](docs/providers.md), and
[verification guide](docs/verification.md).

## Development

Use Python 3.11+, `uv`, and Node.js. Install the locked development dependencies
with `uv sync --frozen --group dev`. Browser tests also require the official
Playwright Chromium revision and supported host libraries. Do not disable the
sandbox or replace the browser to make a test pass.

Make focused changes and add regression tests for changed behavior. Use owned
local fixtures and synthetic data. The normal verifier does not require a live
model key; live provider calls and paid benchmarks are separate, explicit work.

Run the documented full verifier against the final tree and use a fresh output
directory outside the repository. Report passed, failed, blocked and unexecuted
checks separately. An offline pass is not a full browser-suite pass.

## Pull requests

Explain the problem, the change, relevant limitations, and the exact checks run.
Do not present historical benchmark measurements as fresh results. Preserve
provenance when changing benchmark displays or links, and label new experiments
with their actual tested revision and environment.

Keep keys, cookies, private browser data, local account configuration, raw live
traces and unrelated agent working notes out of commits. Follow the
[security policy](SECURITY.md) for vulnerability reports.

The project is licensed under [MIT](LICENSE). Contribute only material you have
the right to submit under that license, and preserve applicable third-party
notices. A license does not establish ownership of screenshots or external data.

# Contributing

Decision Browser is a Rust stdio MCP server. Start with the [README](README.md),
[provider guide](docs/providers.md) and [verification guide](docs/verification.md).

Use Rust 1.100.0 or newer, Cargo and a compatible installed Chrome or Chromium.
Keep the browser sandbox enabled. `DECISION_BROWSER_CHROME` selects an explicit
browser executable when automatic discovery does not find it.

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --all-targets -- --include-ignored --test-threads=1
sh scripts/check-python-free.sh
```

Make focused changes and add regression coverage for changed behavior. Browser
tests use owned local fixtures and synthetic data. Model API acceptance is a
separate run with a recorded provider, model and cost allowance. Report failures
and unexecuted checks alongside passing results.

Explain the problem, resulting behavior, limitations and exact checks in pull
requests. Preserve benchmark provenance and link each new measurement to its
actual revision, environment and attempted cases.

Keep credentials, cookies, private browser data, account configuration and raw
live traces out of commits. Follow [SECURITY.md](SECURITY.md) for reports.
Contribute material you have rights to submit under [MIT](LICENSE), and preserve
applicable [third-party notices](THIRD_PARTY_NOTICES.md).

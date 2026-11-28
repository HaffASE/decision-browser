# Verification

Run verification on the exact tree intended for review or release. Record the
commit, Rust version, operating system, Chrome version and command results.
Cargo.lock pins library resolution. Browser versions and host libraries are
recorded separately.

## Deterministic native checks

Use Rust 1.100.0 or newer, Cargo and an installed compatible Chrome or Chromium.
The browser needs loopback access and a working sandbox. Select a particular
browser executable with `DECISION_BROWSER_CHROME` when comparing measurements.

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --all-targets -- --include-ignored --test-threads=1
sh scripts/check-python-free.sh
cargo run --locked --bin check-native-release
```

`--include-ignored` runs tests that require Chrome. Their default ignored status
keeps a unit-only run usable on machines without a browser. Capture the full
command output and report the executed count. Deterministic tests use synthetic
credentials or owned loopback fixtures and make no paid model requests.

The Python-free check examines tracked runtime, build and packaging files for
Python sources and active bootstrap commands. A packaged lifecycle check also
starts the native executable with an empty executable search path.

## Native archive lifecycle

Choose a fresh output directory outside the checkout.

```sh
sh scripts/build-native-release.sh --output /absolute/path/new-candidate
sh scripts/smoke-native-package.sh /absolute/path/new-candidate
```

The smoke verifies outer and inner checksums, installs both client registrations,
checks doctor output, upgrades, previews recovery and uninstalls while preserving
preexisting user settings. It also asks the unpacked executable for all six MCP
tool definitions. The optional npm wrapper has its own launcher tests.

```sh
node --test npm/test/*.test.cjs
```

## Codex App Server acceptance

The Rust example starts an isolated Codex App Server client, presents standard
MCP forms and checks an owned counter independently. It responds to test forms
programmatically. Native user-click acceptance needs its own client run.

```sh
cargo build --locked --bins --examples
./target/debug/examples/codex_acceptance \
  ./target/debug/decision-browser /absolute/path/new-jev-evidence jev --product
```

The routes `luna` and `gemini` select the other recorded live providers. Each
run uses a fresh evidence directory. It records tools, forms, task status,
provider usage and artifact reads. Run paid acceptance only within the approved
provider allowance.

## Timed browser matrix

The standalone matrix uses the checked-in 21-case corpus, real CDP inputs,
independent page checks and fixture-side event oracles. It selects all cases
unless case names follow the output path.

```sh
cargo build --locked --release --bin browser_matrix
./target/release/browser_matrix --list
./target/release/browser_matrix jev /absolute/path/new-jev.jsonl
./target/release/browser_matrix luna /absolute/path/new-luna.jsonl
./target/release/browser_matrix gemini /absolute/path/new-gemini.jsonl
```

Every attempted case contributes a result row, including failures. Report
confirmation rate beside elapsed time. Keep targeted reruns as additional
attempts, and preserve original full-run failures. This standalone matrix tests
the matrix runner. Use the Codex product acceptance for MCP confirmation and
artifact lifecycle evidence.

## Hosted checks and releases

CI executes native formatting, Clippy, Rust tests and package lifecycle checks
on its configured hosts. Read the exact commit's job results before promotion.
A candidate built for one operating system does not establish browser or
installer acceptance on another. Cross-compiled external executables need
native target validation before publication.

A release requires a Python-free source tree, successful full native checks,
live acceptance, exact version metadata and reviewed artifacts. See the
[release checklist](public-release-checklist.md) and [release procedure](releasing.md).

## Historical evidence

Reports under `docs/benchmarks` retain earlier Python and native measurements
with their original dates and environments. The pre-migration verifier and
source tests remain in [the pinned migration base](https://github.com/HaffASE/decision-browser/tree/7babeb4114130e1bf97bd0f23599405b70965372).

# Reproducible local and CI verification

The [Decisions library comparison](benchmarks/2026-11-02-decisions-library-comparison.md)
documents the separate opt-in, metered live benchmark against jev-ultrafast and
jev-ra. Its adapters, scoring and report aggregation have offline unit tests;
the normal verification command makes no paid model requests.

The [3 November article and context follow-up](benchmarks/2026-11-03-decision-browser-article-cost.md)
records the smaller native Decisions context, all development attempts and the
repeated 144-attempt comparison.

The [owned browser scenario corpus](browser-scenarios.md) documents the portable
workflow, race and stopping cases collected by the full suite, plus the explicit
scripted/configured-provider command for running the same synthetic workflows.

Run the checked-in verifier from a Git checkout with Python 3.11+ and `uv`
available on `PATH`, plus Node.js for the browser-free JavaScript regressions.
The reference environment is CPython 3.12.14, uv 0.13.7, and Node.js 24.19.0;
the report records the actual platform, interpreter, installed distributions,
commit, source manifest, and input hashes. Dependency pins do not by themselves
make different operating systems, Python versions, or Chromium installations
identical.

## Functional check contract

Task requests validate each check before constructing a browser or provider.
Targets have a 512-byte UTF-8 cap. String expectations retain the 4000-character
cap and also have a 4096-byte UTF-8 cap. Route targets must be empty and their
expectation must be a string; text/value expectations are strings, visible
expectations are booleans, and count expectations are nonnegative integers.
DOM targets must be nonempty single CSS selectors. Engine-prefixed selectors and
selector chaining are rejected before startup; CSS syntax is checked by the
browser.

`text` and `value` wait up to 500 ms per selector for attachment. This readiness
wait does not retry until the expected value appears. After readiness, route
and DOM conditions are measured together in one synchronous, isolated-world
observation of the current top-frame document. A route that matched earlier
cannot be combined with DOM text that appeared only after a route change;
DOM-only changes within one document receive the same coherent final read.
The observation returns only match booleans and sanitized reason codes, not
actual page strings.
The stateless verification realm is reused within a document, while each
temporary CDP session is detached after the read. Navigation creates a fresh
document realm; the ordinary DOM action session keeps its own isolated world.

`route`, `visible`, and `count` do not wait for an expected state. Visibility
uses Chromium's rendered visibility and a nonempty box; zero opacity does not
make an element invisible. An absent target is invisible. Text, value, and
visibility checks require exactly one matching element; count measures the
whole matching set. A value check follows a native label to its associated
input, textarea, or select, as the existing locator contract does. These are
read-only checks, and expected-value retries are not part of their contract.

`fixture_state` is supported only by the direct `verify_functional` API with an
explicit trusted adapter's state snapshot. The mapping is copied and checked
before browser waits: at most 64 keys, UTF-8-bounded string keys/values, and
exact string, boolean, or integer scalar values. A malformed map fails with
`fixture_state_invalid`; a missing map fails with `missing_fixture_state`.
Public `TaskRequest` and MCP `start_task` reject `fixture_state` before startup
because the main runner has no trusted fixture adapter. Arbitrary caller state
is not accepted as public verification evidence.

`verified` applies to the declared conditions in the measured final observation.
It does not claim that the page remains unchanged after return, that arbitrary
JavaScript effects are understood, or that external fixture state and DOM share
an atomic storage transaction. Empty checks remain `completed_unverified`.

## Commands

Choose a fresh output directory outside the checkout for each run. The verifier
refuses to overwrite earlier evidence or write its output into the source tree.
`UV_PROJECT_ENVIRONMENT` and `UV_CACHE_DIR` are optional; by default uv uses the
checkout's `.venv` and its normal cache. `PLAYWRIGHT_BROWSERS_PATH` may explicitly
select an already-provisioned Playwright browser cache. Do not share a verification
environment with a concurrent runner: the frozen sync removes undeclared packages and the
verifier installs the wheel under test.

```sh
# Unit-only, cache-only verification. Dependencies and build tools must already
# be cached; missing cached artifacts fail setup rather than falling back online.
python3 -I scripts/verify.py --profile offline --output-dir ../verification-offline

# Full owned-local suite, including the browser prerequisite and pytest tests.
# This is the default profile when --profile is omitted.
python3 -I scripts/verify.py --profile full --output-dir ../verification-full
```

For an explicitly selected environment and cache:

```sh
UV_PROJECT_ENVIRONMENT=/absolute/path/to/verification-venv \
UV_CACHE_DIR=/absolute/path/to/uv-cache \
python3 -I scripts/verify.py --profile full --output-dir /absolute/path/to/new-evidence
```

The full profile may download pinned Python packages during setup. It never
installs browsers automatically. If Chromium is absent, provision the browser
from Playwright's official distribution in an environment where downloading and
running it is permitted, then rerun with a **new** output directory:

```sh
# Run from the checkout with the same selected environment/cache as verification.
uv --no-config --no-python-downloads lock --check
uv --no-config --no-python-downloads sync --frozen --group dev --no-install-project \
  --python "$(python3 -I -c 'import sys; print(sys.executable)')"
"${UV_PROJECT_ENVIRONMENT:-.venv}/bin/python" -I -m playwright install chromium
```

Host libraries and a working Chromium sandbox are separate prerequisites. Do not
substitute an unrelated browser binary or disable the sandbox to turn a blocked
run green. The verifier's launch preflight explicitly enables Chromium's sandbox.
It loads an owned in-memory page and closes the browser; it does not attach to a
user's existing browser. A successful preflight is not a sandbox audit of the
application's separate launch configuration. The isolated production driver and
all integration-test Chromium launches also explicitly request `chromium_sandbox=True`;
a static regression and driver unit tests enforce that request without a fallback.

## Hosted CI

[`.github/workflows/verify.yml`](../.github/workflows/verify.yml) runs the full
profile on Python 3.11, 3.12 and 3.13 for pull requests, pushes to `main`,
`v*` tag pushes, and manual dispatch. Versions and release prerequisites are
checked first; see [Releasing](releasing.md). It has
`contents: read` permission, does not persist checkout credentials, cancels
obsolete runs for the same ref, and limits each job to 45 minutes. It uses no
repository secrets, live API requests, host admission, or existing user browser.
A hosted `PASS` requires a completed successful run for the tested revision.
Adding or statically validating this workflow does not execute it on GitHub.

The workflow pins these official actions to full commit SHAs:

| Action | Release | Commit |
| --- | --- | --- |
| `actions/checkout` | `v6.0.2` | `de0fac2e4500dabe0009e67214ff5f5447ce83dd` |
| `actions/setup-python` | `v6.1.0` | `83679a892e2d95755f2dac6acb0bfd1e9ac5d548` |
| `astral-sh/setup-uv` | `v7.1.6` | `681c641aba71e4a1c380be3ab5e12ad51f415867` |
| `actions/setup-node` | `v6.0.0` | `2028fbc5c25fe9cf00d9f06a71cc4710d4507903` |
| `actions/upload-artifact` | `v6.0.0` | `b7c566a772e6b6bfb58ed0dc250532a479d7789f` |

Python uses a 3.11/3.12/3.13 matrix, Node.js is explicitly selected from the 24
series, and uv is pinned to 0.13.7. The actual
Python patch version, OS, uv, Node.js, and installed distributions are recorded;
the hosted runner image and Python patch are not byte-for-byte pins. The venv,
uv cache, Playwright browser directory, and evidence root are all under
`runner.temp`; the first shell step derives them from `RUNNER_TEMP` and writes
them to `GITHUB_ENV` for subsequent steps. The verifier requires that step to
succeed. This avoids the unavailable `runner` context in job-level `env` (see
[GitHub context availability](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability)).
uv's persistent action cache is disabled. Locked setup uses
`uv --no-config --no-python-downloads lock --check` and a frozen dev sync with
`--no-install-project --python <selected-python>`. The selected venv then runs
`python -I -m playwright install --with-deps chromium`, installing the official
Chromium revision for locked Playwright 1.64.1 and its required host libraries.

The runner is explicitly `ubuntu-22.04`, a supported target in
[Playwright 1.64.1's registry](https://github.com/microsoft/playwright/blob/v1.64.1/packages/playwright-core/src/server/registry/index.ts)
and used by its
[tagged upstream test workflow](https://github.com/microsoft/playwright/blob/v1.64.1/.github/workflows/tests_primary.yml).
Ubuntu documents restricted unprivileged user namespaces from 24.04 in its
[security feature overview](https://documentation.ubuntu.com/security/security-features/security-features-overview/).
Using 22.04 avoids that newer default AppArmor compatibility issue without
altering any host security controls; it does not itself prove this project's
browser preflight will pass. Do not disable the sandbox, change sysctl/AppArmor,
substitute a browser, use privileged containers, or skip tests to obtain green.

**Migration required:** GitHub's
[Ubuntu 22.04 retirement announcement](https://github.com/actions/runner-images/issues/14254)
sets retirement for **2027-04-17**. Before then, select a supported replacement
and establish a fresh sandbox-enabled preflight and full-suite result on its
unchanged security configuration. Merely changing the runner label is not
verification.

Setup and browser-install shell steps use `pipefail` and save tee logs. Even if
browser installation fails, the full verifier still runs once checkout, Python,
and uv are available and runtime paths were initialized, so a missing browser
can yield `BLOCKED` and dependent tests `NOT_RUN`. An earlier failing step keeps
the job failed; no step uses
`continue-on-error`. A setup failure can instead yield `FAIL`, and interrupted
or absent evidence is never PASS.

The always-run upload retains setup/browser/verifier logs,
`verification/result.json`, per-stage `verification/*.log`, and
`verification/pytest-*.xml` for seven days. This evidence artifact also retains
any generated `dist/*.pyz`, `dist/*.tgz`, `installer.sha256`, and `npm.sha256`.
A failed packaging check can leave an incomplete candidate in that upload;
inspect the recorded step outcomes before using it. Wheel and sdist bytes are not
part of this always-run upload; their hashes remain in the report. Missing all
files is an upload error; hidden-file upload is disabled. Browser profiles,
screenshots, arbitrary checkout paths, and private directories are not uploaded.
On a successful Python 3.12 tag build only, a separate 14-day release
artifact contains the tested `*.whl`, `*.tar.gz`, `*.pyz`, `*.tgz`, both packaging
checksum files and result.json. It does not publish a
release or upload to PyPI. This artifact policy
does not authorize uploading a later live run's private evidence.

## What is pinned and checked

The runner performs the following operations, saving each exact command, exit
code, duration, log, and log SHA-256 in `result.json`:

1. Record `uv --version`; `uv lock --check` rejects a stale lockfile.
2. `uv sync --frozen --group dev --no-install-project` creates the runtime/dev
   environment from `uv.lock` without rewriting it. Skipping the editable project
   build prevents an unconstrained build-backend resolution during sync.
3. `uv build --python <environment-python> --build-constraints
   scripts/build-constraints.txt --out-dir <evidence>/dist` builds the sdist and
   wheel. The constraints pin the already-used Hatchling backend and all of its
   Python 3.11+ dependencies; they add no runtime dependencies. These build pins
   constrain versions, not artifact hashes. `uv.lock` remains
   unchanged. Build constraints are separate because PEP 517 isolated build
   environments are not covered by the runtime lock. See
   [uv's build constraints documentation](https://docs.astral.sh/uv/concepts/resolution/#build-constraints).
4. Install the **newly built wheel** with `uv pip install --no-deps --no-index
   --reinstall`, then record all installed package versions and run `uv pip check`.
   Test commands use that environment's Python with `-I`; the import smoke check
   verifies that `decision_browser` resolves inside the environment, not from the
   source checkout or an old editable install. This is a separate installed-wheel
   import check. The test suite mixes installed-package imports with intentional
   source-level checks: JavaScript regressions load repository assets, and MCP
   stdio tests set their own source `PYTHONPATH`. It is not a wheel-only suite.
5. Ruff checks `src`, `tests`, `scripts`, the Hatch hook and shipped skill doctor.
   Mypy checks `src/decision_browser`, the verifier, installer/runtime preparer,
   release/build scripts, shared build metadata and shipped skill doctor.
   This is a bounded gate for authoritative tooling, without an arbitrary strict-all
   or 100% coverage requirement. Import smoke covers the browser, CLI, server, and direct
   dependencies. Node.js must be available; its version is recorded.
   `pytest tests/unit` runs in both profiles. A zero exit code alone is insufficient:
   the JUnit report must contain tests with no failures, errors, or skipped tests.
6. Only the full profile launches the browser prerequisite and, if that succeeds,
   runs `pytest tests`. On Windows only, both pytest commands explicitly deselect
   the reviewed POSIX-only cases: nine process-group cases in
   `test_verification_harness.py::test_timeout_cleanup_is_bounded_and_preserves_timeout`,
   18 private-profile/symlink/permission fixtures and 11 installer process/kernel/dir_fd
   fixtures, plus one installer lifecycle crash fixture (39 exclusions in total).
   Portable cases in those modules stay enabled.
   `result.json.platform_exclusions` records
   each exact parametrized node ID and reason before execution. A regression
   compares this list with pytest's actual collection; newly added cases are never
   implicitly excluded. All other
   skips still fail the JUnit coverage gate; this is not native Windows qualification.
   No marker/name filter is otherwise applied. If launch fails, it records
   `browser_preflight=BLOCKED` and `pytest_full=NOT_RUN`, preserving the launch log.

The full profile intentionally repeats the unit subset after its standalone
diagnostic stage. The standalone stage localizes browser-free failures; the full
stage proves the complete collected suite in one process. Compare exact collected
node IDs before changing this policy; a timing estimate alone does not prove
equivalent coverage. New evidence and any future disjoint-stage design must retain
an explicit union coverage manifest. All tracked `scripts/` files participate in
source identity, including the diagnostic scenario and Flights probes. Arbitrary
untracked files remain unverified and block the run without being opened.
7. Compare source manifests and Git identity before/after the run. Any drift in
   this recorded identity fails `source_unchanged`. The manifest hashes public
   tracked source (`src`, `tests`, `skills`, `examples`,
   `docs`, `npm`, CI workflows, named scripts and root project files) and the explicitly
   named new verifier assets. Private/runtime roots, including `.omx` and `.codex`,
   are never opened or exported by the manifest; only their excluded count is
   recorded. Changes within excluded tracked files are not detected when the
   checkout was already dirty; this gate attests only the recorded public manifest
   and Git identity, not private content. Any other nonignored untracked files
   fail verification without opening
   their contents; commit intended source first. Ignored runtime files are outside
   this check. A dirty checkout is allowed but
   explicitly recorded, so its commit SHA alone must not be cited as the tested
   source. The source-tree hash and per-file manifest identify what was tested.

The runner starts child environments from an explicit allowlist of OS runtime
paths, home/cache/temp locations, and locale settings, plus the three optional
locations above. Arbitrary inherited credentials, proxy URLs, provider settings,
and tool/browser overrides are not forwarded. This limits ambient inheritance;
it is not filesystem or network isolation from the user's account.
The runner sets `SOURCE_DATE_EPOCH` from the commit for builds and disables pytest's
automatic third-party plugin discovery. It explicitly loads the pinned
pytest-asyncio plugin. Behavior-changing `UV_*` variables are not inherited; uv
commands are anchored to this checkout with `--directory` and `--project`, and
`--no-config` disables ambient uv settings. Git identity commands use only the OS
runtime allowlist, without `GIT_*` selectors that could redirect the checkout.
It never prints the inherited environment. Offline mode disables uv network resolution;
it is a unit-test selection, **not an operating-system network sandbox**.

Every stage has a configurable finite positive timeout (`--timeout`, default 900
seconds). On timeout, the runner requests termination of the child (its owned
process group on POSIX), then waits at most five additional seconds to reap the
leader. Timeout evidence preserves `reason=TimeoutExpired` and records `cleanup`:
`LEADER_REAPED` confirms only the leader was reaped, not all descendants;
`UNKNOWN` means reaping timed out; `ERROR` records a termination or wait error.
Unknown/error cleanup is a failing gate, even for a prerequisite stage. Missing
or invalid pytest XML is recorded separately as `junit_reason` without replacing
the primary command failure. Logs and incremental `result.json` survive failed
stages. An interrupted report with
`status=RUNNING`, a missing final `exit_code`, or missing evidence is incomplete.

## Reading the result

- Exit 0 / `status=PASS`: all required stages for the named profile passed.
- Exit 1 / `status=FAIL`: a setup, build, lint, typing, import, dependency, test,
  or source-integrity gate failed. Other independent checks run where possible.
- Exit 2 / `status=BLOCKED`: a Node.js or Chromium prerequisite failed and
  no other gate failed. Dependent pytest stages did **not** run. A separate failed
  gate takes precedence and produces exit 1; the prerequisite block remains recorded.
- Exit 3: invalid command-line arguments, including an existing/in-checkout output
  directory or a nonpositive/nonfinite timeout. Verification does not start and
  no `result.json` is created. Exit 2 is reserved for a written `BLOCKED` report.
- `full_suite_passed` can be true only for a passing full profile that actually
  executed full pytest. An offline PASS always has `full_suite_passed=false`.

The output contains `result.json`, hashed per-stage logs, built artifact hashes,
and JUnit XML for each executed pytest stage. The environment log is JSON and
lists exact installed versions. Keep this directory as evidence for the specific
commit and source-tree hash; do not commit it or reuse historical counts as a
fresh result. The wheel and sdist are hashed outputs, not a promise of
byte-identical builds across arbitrary platforms.

Neither profile proves native host integration, human admission, a live provider
request, existing-browser behavior, or production readiness. Those acceptance
areas remain explicitly `NOT_RUN` in the report. Mock admission tests stay mock
admission evidence. An old full-suite log is historical even if today's offline
profile passes.

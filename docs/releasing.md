# Versioning and release builds

`project.version` in `pyproject.toml` is the source of truth. The package's
`__version__` reads installed distribution metadata; an uninstalled source tree
reports `0+unknown`. `uv.lock` must record the same project version. CI checks
this without contacting any registry.

Use `MAJOR.MINOR.PATCH`; pre-1.0 breaking changes increment MINOR, fixes increment
PATCH. After 1.0, breaking API changes increment MAJOR. Optional Python
prereleases use `0.2.0rc1`, `0.2.0a1` or `0.2.0b1`. Git tags use the exact version
with a `v` prefix, for example `v0.2.0rc1`. The npm package uses the explicit
project mapping `aN` → `-alpha.N`, `bN` → `-beta.N`, and `rcN` → `-rc.N`:
`0.2.0rc1` therefore corresponds to `0.2.0-rc.1`. Stable versions are identical.
`scripts/build_metadata.py` defines this narrow version contract for both release
checking and npm builds. Epochs, post/dev/local versions, aliases, and other
noncanonical forms are rejected. This is a project mapping, not a general
equivalence between [PEP 440](https://peps.python.org/pep-0440/) and
[SemVer](https://semver.org/#spec-item-9). Versions are chosen deliberately by a
maintainer rather than inferred from every commit message.

## Prepare a release

1. Review public-readiness findings, tracked files and Git history. Preserve the
   owner-selected MIT license, SPDX metadata and third-party notices; confirm
   rights to all distributed code and assets.
2. Update the version and lockfile using `uv version 0.2.0` (example only).
   Review the diff; do not upgrade unrelated dependencies. Run `uv lock --check`.
   Keep `npm/package.json` at the mapped version above; the release checker and
   launcher builder reject a mismatch.
3. Move release notes from `[Unreleased]` to a dated heading such as
   `## [0.2.0] - 2026-11-04`, describing changes, compatibility and known limits.
4. Run `python -I scripts/check_release.py --tag v0.2.0`, then the full verifier
   from [Verification](verification.md). Commit the reviewed changes and obtain
   a successful hosted Verify run on that exact commit before tagging it.
5. Only when publication is explicitly authorized, create and push an annotated
   `v<version>` tag. Do not move or overwrite an existing release tag.

Tag pushes run the same full verification workflow as pull requests. The tag
must match metadata, with a license and dated changelog entry. Only a successful
Python 3.12 tag build uploads the tested wheel and sdist as a separate `release-candidate-*` Actions
artifact. That artifact alone is not release approval: all matrix jobs and the
downstream **Release candidate gate** must succeed before publication. The verifier's `result.json` records their SHA-256 checksums. No job
creates a GitHub Release, uploads to PyPI, changes repository visibility or uses
publish credentials. Publication is a separate, reviewed step.

Review the release artifacts and obtain explicit publication approval for each
version. The version in the source tree identifies the candidate being prepared;
verify the published tag and registry package when checking release availability.

## Build locally

Use a clean checkout of the reviewed commit for release builds. Check
`git status --short` before building and resolve any intended source changes.
Keep local benchmark output outside the checkout. Generated
`docs/benchmarks/**/output/` directories are excluded from source distributions;
the curated reports, data and assets remain included.

Hatchling is the project's PEP 517 backend. Build this pure-Python distribution
with the pinned build dependencies:

```sh
uv build --no-sources --build-constraints scripts/build-constraints.txt
```

This produces `dist/*.whl` and `dist/*.tar.gz`. Prefer the verifier for release
candidates: it uses an external output directory, installs the exact wheel,
checks imports, dependencies, lint, typing, unit and local browser tests, and
records package hashes. Browsers are not bundled in the Python package; the
consumer provisions the pinned Playwright Chromium separately.

Reference: [uv package building and version updates](https://docs.astral.sh/uv/guides/package/).

## Standalone installation artifact

After the verifier builds and tests a wheel, create the checkout-free installer:

```sh
python3.12 scripts/build_installer.py --wheel /path/to/decision_browser-0.1.3-py3-none-any.whl --output /path/to/decision-browser-0.1.3.pyz
```

Use the actual selected version in both names. The builder refuses to overwrite
an existing artifact. It bundles the wheel's reviewed source inputs, frozen lock,
licenses and skills, with a stdlib-only bootstrap. The code-owned input list in
`scripts/build_metadata.py` is shared by Hatch and installer preflight; missing
README, notices, build constraints, hook, metadata helper, or required entrypoint
is rejected before creating a candidate. No dependencies are resolved at
archive build time. Test the archive from a clean directory and record its SHA-256
alongside the tested wheel/sdist before approving distribution. Publication needs
a real immutable URL and separately approved release; this command publishes nothing.

## npm launcher candidate

The npm launcher is a distribution wrapper, not a rewrite of the Python server.
Its registry name is `@haffa/decision-browser`; confirm registry ownership and
availability before approving any publication. There are no npm dependencies or
lifecycle hooks. Build only from the wheel that passed the release checks:

```sh
python3 -I scripts/build_npm.py --wheel /path/to/tested/decision_browser-0.1.3-py3-none-any.whl --output /path/to/new/npm-candidate
cd /path/to/new/npm-candidate
npm pack --offline --ignore-scripts --pack-destination /path/to/artifacts
npm exec --offline --yes --package=/path/to/artifacts/haffa-decision-browser-0.1.3.tgz -- decision-browser --help
```

The builder refuses an existing output directory and mismatched wheel/launcher
versions. It copies license notices from the wheel, creates the `.pyz` from its
bundled build inputs, and records its SHA-256. Inspect the tarball file list and
checksum before handing it to a user. The launcher uses the embedded Python
lockfile rather than resolving a same-named PyPI package.

The package also includes the uv bootstrap, bounded archive downloader and pinned
platform SHA-256 manifest. uv itself is downloaded from Astral when needed, not
bundled. The wheel's bootstrap inputs include `prepare_tool_runtime.py`, allowing
the packed launcher to run `configure` and `doctor` from a verified command cache
without rebuilding the browser installation. For acceptance, remove uv from the
test process PATH and select a fresh `DECISION_BROWSER_CACHE_DIR`; exercise setup,
configure and JSON doctor using the packed artifact from outside the checkout.

The unit suite runs the Node regressions. CI builds and packs the launcher from
the verified wheel, checks the packed command through offline npm, and uploads
it as a candidate only. No workflow publishes to npm or PyPI or creates a GitHub
release. A candidate is not a native end-to-end installation pass: test clean
install, repeat, upgrade, removal and interrupted setup on every claimed OS,
with project paths containing spaces, before publishing. Client trust and native
admission must be reviewed normally. Do not use real model keys for setup tests.

## Publish and verify the npm release

For `@haffa/decision-browser@0.1.3`, confirm write access to the npm
scope and complete the [public-release checklist](public-release-checklist.md).
After publication approval and a passing tag matrix and release-candidate gate,
publish the reviewed tarball from that tag. Compare the full `integrity` in
the dry-run JSON for that exact tarball with the registry's `dist.integrity`.
See the [npm guide to scoped public packages](https://docs.npmjs.com/creating-and-publishing-scoped-public-packages/).

```sh
tarball="/path/to/haffa-decision-browser-0.1.3.tgz"
npm publish "$tarball" --access public
npm pack --offline --ignore-scripts --dry-run --json "$tarball"
npm view @haffa/decision-browser@0.1.3 dist.integrity
```

After the exact version is available in the registry, verify this command in an
empty test project, then use it in the public quick start:

```sh
npx --yes @haffa/decision-browser@0.1.3 setup codex
```

The user needs Node.js 20+ and the Codex CLI on PATH. Guided setup installs the
runtime, Chromium, MCP registration and skill, then asks for a provider. Use
`setup claude` or `setup both` for the other client choices. Restart the client
after setup and review its trust, MCP and task-admission prompts as they appear.
For a reviewed tarball, use the [local artifact installation commands](installation.md#guided-setup).

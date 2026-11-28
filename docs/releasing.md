# Releases

The native version in Cargo.toml is the version source. Cargo.lock must contain
the same local package version. Release tags use the exact `vMAJOR.MINOR.PATCH`
form. Update version metadata deliberately and add one dated changelog heading.

Before 1.0, incompatible runtime, installation or public behavior changes
increment MINOR. Patches within that line increment PATCH. Version 0.2.0 assigns
the Rust migration its compatibility boundary. Historical 0.1.x tags remain
available and published releases retain their original artifacts.

## Verify a candidate

Run the complete [verification procedure](verification.md) on the exact reviewed
commit. Require the tracked tree to pass the Python-free gate, the native
installer lifecycle, all six MCP tools, the full browser suite and the authorized
live provider runs. Retain failed attempts alongside reruns.

```sh
sh scripts/check-python-free.sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --all-targets -- --include-ignored --test-threads=1
cargo run --locked --bin check-native-release -- --tag vVERSION
```

Replace `vVERSION` with the reviewed package version. The native checker verifies
Cargo metadata, Cargo.lock, the MIT license and the exact dated changelog entry.
The release gate requires the full final tree before any tag can proceed.

## Build reviewed artifacts

Use a clean checkout and a fresh output directory.

```sh
sh scripts/build-native-release.sh --tag vVERSION --output /absolute/path/new-candidate
sh scripts/smoke-native-package.sh /absolute/path/new-candidate
```

The builder checks metadata and the executable's target. Host executables also
have their reported version checked. Verify any supplied cross-target binary on
its native target before publishing it. Keep SHA-256 manifests with the exact
archives that passed installation, upgrade, recovery and uninstall acceptance.

The native archive includes the executable, manifest, licenses and checksums.
Use `--npm` to prepare the optional local npm package. Verify its launcher and
packaged executable as well. Node.js is needed to build or run that wrapper.

## Build the public npm package

Use the macOS arm64 and Linux x64 native candidates from successful CI jobs for
the same reviewed commit. Assemble both into one public package in a fresh
output directory.

```sh
sh scripts/build-npm-release.sh MACOS_CANDIDATE_DIR LINUX_CANDIDATE_DIR NEW_OUTPUT_DIR
```

The public package selects the matching bundled executable at launch. Check its
contents, version and hashes, and test that exact tarball on both supported
platforms before publication. Retain the npm candidate alongside the two native
candidates and their verification evidence.

After authorization and npm login, publish the verified tarball and compare the
registry integrity digest with the local file. Test a fresh installation from
the registry and verify the published version and `latest` tag.

## Promote a release

Read hosted verification results for the exact commit and complete the
[release checklist](public-release-checklist.md). Review Sol and Astra findings
and resolve release blockers. With publication authorization, create an annotated
immutable tag, inspect its completed checks and publish the tested assets.
Verify the resulting tag, download hashes and published package metadata.

A successful job produces evidence for its tested host and artifact. Add each
supported platform only after its native browser and installer checks pass.

## Historical versions

The v0.1.3 and earlier Python package versions, installers and build procedures
are preserved in their [immutable release tags](https://github.com/HaffASE/decision-browser/releases).
Use those tags to reproduce historical artifacts and measurements.

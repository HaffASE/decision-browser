# Install a native Decision Browser candidate

Use a reviewed archive matching your operating system and CPU. The native
archive contains a Rust executable, license notices, a manifest and SHA-256
checksums. Chrome is a separate prerequisite. The build script creates local
candidates and performs no publication.

## Build a candidate

A source build requires Rust 1.100.0 or newer, Cargo, `tar`, `file`, and either
`sha256sum` or `shasum`. The first build may download the dependencies pinned in
Cargo.lock. A reviewed executable can also be supplied with `--binary`.
Run the following from the repository root with a fresh output directory.

```sh
sh scripts/build-native-release.sh --output /absolute/path/new-native-candidate
```

The default builder locates the root Cargo manifest, or the migration crate
under `prototypes/rust-cdp-counter` while it exists. It builds the
`decision-browser` binary for the Rust host target. `--manifest-path`,
`--target` and `--version` select explicit build inputs. An explicit binary
must already have been built for the supplied target. For host executables,
the builder also checks that `--version` matches the executable version.

```sh
sh scripts/build-native-release.sh \
  --binary /absolute/path/reviewed/decision-browser \
  --target aarch64-apple-darwin --version 0.1.4 \
  --output /absolute/path/new-native-candidate
```

## Verify and install

Obtain the archive and its outer `SHA256SUMS` through a trusted maintainer.
Check the outer archive before extraction, then check the enclosed executable.
For macOS, use `shasum -a 256 -c SHA256SUMS`; systems providing GNU coreutils can
use `sha256sum -c SHA256SUMS`.

```sh
cd /absolute/path/new-native-candidate
shasum -a 256 -c SHA256SUMS
tar -xzf decision-browser-VERSION-TARGET.tar.gz
cd decision-browser-VERSION-TARGET
shasum -a 256 -c SHA256SUMS
./bin/decision-browser --help
./bin/decision-browser install codex --project /absolute/path/project
```

Choose `claude` or `both` when the corresponding clients are installed. Use
`serve --admission standard_form` for direct stdio MCP registration. The native
installer copies its executable to a persistent project runtime before writing
managed client configuration. `doctor` reports local installation state.
Consult the CLI's help for current configuration and recovery commands.

The archive can be copied, extracted, and started on a prepared machine without
Python, uv, Node.js, or package downloads. Keep Chrome and the coding client
installed before running a browser task. The live provider still needs network
access and credentials when the task makes an API request.

## Browser and platform prerequisites

The counter and matrix have native execution evidence on macOS arm64 with an
installed Chrome. Set `DECISION_BROWSER_CHROME` to the browser executable when
it is outside the discovery paths. For example, Chrome for Testing uses an
executable beneath its `.app/Contents/MacOS` directory. Browser discovery
requires an executable file, and runs use a fresh profile with Chrome's sandbox.

Linux needs a compatible Chrome/Chromium build, its system libraries, and a
working Chrome sandbox. Building the Rust executable does not establish native
browser or installer acceptance on Linux. The builder also recognizes macOS
x64, Linux x64/arm64 and Windows x64/arm64 target identifiers for preparing a
matching candidate. Native installation and browser acceptance must be verified
on each platform before publishing support. Shell build commands require a
POSIX build environment, including when packaging a Windows executable.

The macOS live routes load supported named credentials from Keychain. Other
platforms require the production provider configuration supported by the native
CLI. `capabilities` and `doctor` report prerequisites before a task starts.

## Optional npm candidate

Pass `--npm` to assemble a private local npm package with the same executable.
This additional build step requires Node.js 20+ and npm. Its launcher verifies
the included executable against the manifest and forwards the CLI arguments
without shell interpolation. The package declares no npm dependencies or
lifecycle install hooks.

```sh
sh scripts/build-native-release.sh --npm --output /absolute/path/new-candidate
npm exec --offline --yes \
  --package=/absolute/path/new-candidate/haffa-decision-browser-native-VERSION.tgz \
  -- decision-browser --help
```

Verify the package's outer SHA-256 as well. The local package is specific to its
manifest platform and architecture. The native archive remains available for
direct execution.

## Native release metadata check

The candidate builder runs the Rust `check-native-release` command before
packaging. It checks the native Cargo package version against Cargo.lock and
requires the package and root license to declare MIT. Use `--tag vVERSION`
when preparing a release. That mode also requires exactly one matching dated
CHANGELOG heading with a valid calendar date.

```sh
cargo run --locked --offline --manifest-path prototypes/rust-cdp-counter/Cargo.toml \
  --bin check-native-release -- --tag v0.1.4
sh scripts/build-native-release.sh --tag v0.1.4 --output /absolute/path/new-release
```

The legacy release checker remains available for its existing package workflow
until the native release switchover.

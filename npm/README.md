# Optional native npm launcher

This package is assembled locally by `scripts/build-native-release.sh --npm`.
It contains the Rust executable for one operating system and architecture and
checks that file's SHA-256 before executing it. This launcher requires Node.js 20+.
For direct installation, use the [v0.2.0 release archives](https://github.com/HaffASE/decision-browser/releases/tag/v0.2.0).

The packed candidate has no lifecycle hooks or npm dependencies. Installation
and launch use only the bundled executable and local files. Download the
candidate from a trusted maintainer and verify its outer SHA-256 before use.
The embedded hash detects mismatches within a bundle; publisher trust comes
from the channel supplying the outer archive and checksum.

Use a fresh directory and the exact local tarball to avoid registry resolution.

```sh
npm exec --offline --yes --package=/absolute/path/haffa-decision-browser-0.2.0.tgz -- decision-browser --version
```

Install Chrome separately and set `DECISION_BROWSER_CHROME` to its executable
when automatic discovery is insufficient. Native client registration and
provider configuration are performed by the Rust CLI. This candidate template
is private and is not published by the build script.

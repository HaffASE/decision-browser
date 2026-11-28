# Native candidate launcher

This package is assembled locally by `scripts/build-native-release.sh --npm`.
It contains the Rust executable for one OS/architecture and checks that file's
SHA-256 before executing it. Node.js 20+ runs this optional launcher. Native
archives can run the executable directly.

The packed candidate has no lifecycle hooks or npm dependencies. Installation
and launch use only the bundled executable and local files. Download the
candidate from a trusted maintainer and verify its outer SHA-256 before use.
The embedded hash detects mismatches within a bundle; publisher trust comes
from the channel supplying the outer archive and checksum.

Use a fresh directory and the exact local tarball to avoid registry resolution.

```sh
npm exec --offline --yes --package=/absolute/path/haffa-decision-browser-native-VERSION.tgz -- decision-browser --help
```

Install Chrome separately and set `DECISION_BROWSER_CHROME` to its executable
when automatic discovery is insufficient. Native client registration and
provider configuration are performed by the Rust CLI. This candidate template
is private and is not published by the build script.

#!/bin/sh
# Assemble the registry package from the two verified native candidates.
set -eu

if [ "$#" -ne 3 ]; then
    printf '%s\n' 'Usage: build-npm-release.sh MACOS_CANDIDATE_DIR LINUX_CANDIDATE_DIR NEW_OUTPUT_DIR' >&2
    exit 2
fi
macos_candidate=$1
linux_candidate=$2
output=$3
[ ! -e "$output" ] || { printf '%s\n' 'Output directory already exists.' >&2; exit 2; }
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$(sed -n 's/^version = "\([0-9][0-9.]*\)"$/\1/p' "$root/Cargo.toml")
[ -n "$version" ] || { printf '%s\n' 'A native release version is required.' >&2; exit 2; }

if command -v sha256sum >/dev/null 2>&1; then
    hash() { sha256sum "$1" | awk '{print $1}'; }
else
    hash() { shasum -a 256 "$1" | awk '{print $1}'; }
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/decision-browser-npm.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
package="$work/package"
mkdir -p "$package/bin" "$package/native"

bundle_native() {
    candidate=$1
    target=$2
    platform=$3
    arch=$4
    bundle="decision-browser-$version-$target"
    archive="$candidate/$bundle.tar.gz"
    expected=$(awk -v file="$bundle.tar.gz" '$2 == file {print $1}' "$candidate/SHA256SUMS")
    if [ -z "$expected" ] || [ "$(hash "$archive")" != "$expected" ]; then
        printf 'Native archive checksum mismatch for %s\n' "$target" >&2
        exit 1
    fi
    destination="$package/native/$platform-$arch"
    mkdir "$destination"
    tar -xOf "$archive" "$bundle/manifest.json" > "$destination/manifest.json"
    tar -xOf "$archive" "$bundle/bin/decision-browser" > "$destination/decision-browser"
    chmod 755 "$destination/decision-browser"
    node - "$destination/manifest.json" "$version" "$target" "$platform" "$arch" "$(hash "$destination/decision-browser")" <<'JS'
const fs = require('node:fs');
const [path, version, target, platform, arch, sha256] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(path, 'utf8'));
if (manifest.format !== 1 || manifest.version !== version || manifest.target !== target ||
    manifest.platform !== platform || manifest.arch !== arch ||
    manifest.file !== 'decision-browser' || !/^[a-f0-9]{64}$/.test(sha256) || manifest.sha256 !== sha256) {
  throw new Error(`Native manifest mismatch for ${target}`);
}
JS
}

bundle_native "$macos_candidate" aarch64-apple-darwin darwin arm64
bundle_native "$linux_candidate" x86_64-unknown-linux-gnu linux x64
cp "$root/npm/bin/decision-browser.cjs" "$package/bin/"
chmod 755 "$package/bin/decision-browser.cjs"
cp "$root/npm/README.md" "$root/LICENSE" "$root/THIRD_PARTY_NOTICES.md" "$package/"
node - "$root/npm/package.json" "$package/package.json" "$version" <<'JS'
const fs = require('node:fs');
const [source, destination, version] = process.argv.slice(2);
const metadata = JSON.parse(fs.readFileSync(source, 'utf8'));
metadata.version = version;
metadata.description = 'Native Rust MCP server for browser tasks with Codex and Claude Code';
delete metadata.private;
delete metadata.scripts;
fs.writeFileSync(destination, JSON.stringify(metadata, null, 2) + '\n');
JS

mkdir "$output"
output=$(CDPATH= cd -- "$output" && pwd)
(cd "$package" && npm --cache "$work/cache" pack --offline --ignore-scripts --pack-destination "$output")
archive="haffa-decision-browser-$version.tgz"
test -f "$output/$archive"
printf '%s  %s\n' "$(hash "$output/$archive")" "$archive" > "$output/SHA256SUMS"

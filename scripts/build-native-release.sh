#!/bin/sh
# Build a local candidate containing the reviewed native executable.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$script_dir/.." && pwd)
manifest="$root/Cargo.toml"
binary=
target=
version=
tag=
output=
with_npm=false
usage() {
    printf '%s\n' 'Usage: build-native-release.sh --output NEW_DIRECTORY [--manifest-path Cargo.toml] [--binary EXECUTABLE] [--target RUST_TARGET] [--version VERSION] [--tag vVERSION] [--npm]'
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest-path|--binary|--target|--version|--tag|--output)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            option=$1
            shift
            case "$option" in
                --manifest-path) manifest=$1 ;;
                --binary) binary=$1 ;;
                --target) target=$1 ;;
                --version) version=$1 ;;
                --tag) tag=$1 ;;
                --output) output=$1 ;;
            esac
            ;;
        --npm) with_npm=true ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done
[ -n "$output" ] || { usage >&2; exit 2; }
[ ! -e "$output" ] || { printf '%s\n' 'Output directory already exists.' >&2; exit 2; }
if [ -n "$tag" ]; then
    (cd "$root" && sh scripts/check-python-free.sh)
fi
if [ -z "$target" ]; then
    target=$(rustc -vV | sed -n 's/^host: //p')
fi
case "$target" in
    aarch64-apple-darwin) platform=darwin; arch=arm64 ;;
    x86_64-apple-darwin) platform=darwin; arch=x64 ;;
    x86_64-unknown-linux-gnu|x86_64-unknown-linux-musl) platform=linux; arch=x64 ;;
    aarch64-unknown-linux-gnu|aarch64-unknown-linux-musl) platform=linux; arch=arm64 ;;
    x86_64-pc-windows-msvc) platform=win32; arch=x64 ;;
    aarch64-pc-windows-msvc) platform=win32; arch=arm64 ;;
    *) printf '%s\n' 'Unsupported bundle target.' >&2; exit 2 ;;
esac
if [ -z "$version" ]; then
    package_id=$(cargo pkgid --manifest-path "$manifest")
    version=${package_id##*@}
fi
case "$version" in
    ''|*[!0-9A-Za-z.+-]*) printf '%s\n' 'Invalid release version.' >&2; exit 2 ;;
esac
set -- --manifest-path "$manifest" --root "$root"
if [ -n "$tag" ]; then set -- "$@" --tag "$tag"; fi
checked_version=$(cargo run --quiet --locked --manifest-path "$manifest" --bin check-native-release -- "$@")
[ "$checked_version" = "Version OK: $version" ] || {
    printf '%s\n' 'Bundle version does not match the checked native package.' >&2
    exit 2
}
if [ -z "$binary" ]; then
    cargo build --locked --release --manifest-path "$manifest" --target "$target" --bin decision-browser
    target_directory=${CARGO_TARGET_DIR:-"$(dirname -- "$manifest")/target"}
    binary="$target_directory/$target/release/decision-browser"
    [ "$platform" != win32 ] || binary="$binary.exe"
fi
[ -f "$binary" ] && [ ! -L "$binary" ] || { printf '%s\n' 'A regular native executable is required.' >&2; exit 2; }
if [ "$platform" != win32 ]; then
    [ -x "$binary" ] || { printf '%s\n' 'Native file is not executable.' >&2; exit 2; }
fi
command -v file >/dev/null 2>&1 || { printf '%s\n' 'The file utility is required to check the executable target.' >&2; exit 2; }
binary_format=$(file -b "$binary")
case "$target:$binary_format" in
    aarch64-apple-darwin:*Mach-O*arm64*|x86_64-apple-darwin:*Mach-O*x86_64*) ;;
    x86_64-unknown-linux-*:*ELF*x86-64*|aarch64-unknown-linux-*:*ELF*ARM*aarch64*) ;;
    x86_64-pc-windows-msvc:*PE32+*x86-64*|aarch64-pc-windows-msvc:*PE32+*Aarch64*) ;;
    *) printf '%s\n' 'Executable format does not match the declared target.' >&2; exit 2 ;;
esac
host_target=$(rustc -vV | sed -n 's/^host: //p')
if [ "$target" = "$host_target" ]; then
    binary_version=$("$binary" --version)
    [ "$binary_version" = "decision-browser $version" ] || {
        printf '%s\n' 'Native executable version does not match the bundle version.' >&2
        exit 2
    }
fi
if command -v sha256sum >/dev/null 2>&1; then
    hash() { sha256sum "$1" | awk '{print $1}'; }
else
    command -v shasum >/dev/null 2>&1 || { printf '%s\n' 'A SHA-256 tool is required.' >&2; exit 2; }
    hash() { shasum -a 256 "$1" | awk '{print $1}'; }
fi
if "$with_npm"; then command -v npm >/dev/null 2>&1; fi
mkdir -p "$output"
output=$(CDPATH= cd -- "$output" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/decision-browser-native.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
bundle="decision-browser-$version-$target"
mkdir -p "$work/$bundle/bin"
executable=decision-browser
[ "$platform" != win32 ] || executable=decision-browser.exe
cp "$binary" "$work/$bundle/bin/$executable"
chmod 755 "$work/$bundle/bin/$executable"
cp "$root/LICENSE" "$root/THIRD_PARTY_NOTICES.md" "$work/$bundle/"
cp "$root/docs/native-installation.md" "$work/$bundle/README.md"
digest=$(hash "$work/$bundle/bin/$executable")
printf '%s  %s\n' "$digest" "bin/$executable" > "$work/$bundle/SHA256SUMS"
printf '{"format":1,"version":"%s","target":"%s","platform":"%s","arch":"%s","file":"%s","sha256":"%s"}\n' \
    "$version" "$target" "$platform" "$arch" "$executable" "$digest" > "$work/$bundle/manifest.json"
tar -czf "$output/$bundle.tar.gz" -C "$work" "$bundle"
printf '%s  %s\n' "$(hash "$output/$bundle.tar.gz")" "$bundle.tar.gz" > "$output/SHA256SUMS"
if "$with_npm"; then
    package="$output/npm"
    mkdir -p "$package/bin" "$package/native"
    cp "$root/npm/bin/decision-browser.cjs" "$package/bin/"
    chmod 755 "$package/bin/decision-browser.cjs"
    cp "$work/$bundle/bin/$executable" "$package/native/"
    cp "$work/$bundle/manifest.json" "$package/native/manifest.json"
    sed "s/\"version\": \"0.0.0\"/\"version\": \"$version\"/" "$root/npm/package.json" > "$package/package.json"
    cp "$root/npm/README.md" "$root/LICENSE" "$root/THIRD_PARTY_NOTICES.md" "$package/"
    (cd "$package" && npm --cache "$work/npm-cache" pack --offline --ignore-scripts --pack-destination "$output")
    for archive in "$output"/*.tgz; do
        printf '%s  %s\n' "$(hash "$archive")" "$(basename -- "$archive")" >> "$output/SHA256SUMS"
    done
fi
printf 'Native candidate saved in %s\n' "$output"

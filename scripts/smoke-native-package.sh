#!/bin/sh
# Verify a locally built archive and its project installation lifecycle.
set -eu

[ "$#" -eq 1 ] || { printf '%s\n' 'Usage: smoke-native-package.sh CANDIDATE_DIRECTORY' >&2; exit 2; }
candidate=$(CDPATH= cd -- "$1" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/decision-browser-smoke.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
if command -v sha256sum >/dev/null 2>&1; then
    verify_hashes() { sha256sum -c SHA256SUMS; }
else
    verify_hashes() { shasum -a 256 -c SHA256SUMS; }
fi
(cd "$candidate" && verify_hashes)
set -- "$candidate"/decision-browser-*.tar.gz
[ "$#" -eq 1 ] && [ -f "$1" ] || { printf '%s\n' 'Expected one native archive.' >&2; exit 2; }
archive=$1
if tar -tzf "$archive" | LC_ALL=C grep -E '\.(py|pyi|pyw|pyz|whl)$|(^|/)(pyproject\.toml|uv\.lock)$'; then
    printf '%s\n' 'Archive contains Python files.' >&2
    exit 1
fi
tar -xzf "$archive" -C "$work"
set -- "$work"/decision-browser-*
[ "$#" -eq 1 ] && [ -d "$1" ] || { printf '%s\n' 'Expected one unpacked native bundle.' >&2; exit 2; }
bundle=$1
(cd "$bundle" && verify_hashes)
binary="$bundle/bin/decision-browser"
[ -f "$binary" ] || binary="$binary.exe"
"$binary" --version

project="$work/project with spaces"
mkdir -p "$project/.codex" "$work/empty-path"
printf 'model = "preserved-user-model"\n' > "$project/.codex/config.toml"
printf '{"mcpServers":{"other":{"command":"preserved-server"}},"user":true}\n' > "$project/.mcp.json"
cp "$project/.codex/config.toml" "$work/codex-before"
for command in install upgrade; do
    PATH="$work/empty-path" "$binary" "$command" both --project "$project"
    PATH="$work/empty-path" "$binary" doctor --json --project "$project" > "$work/doctor.json"
    grep -q '"status":"clear"' "$work/doctor.json"
    grep -q '"registration_matches":true' "$work/doctor.json"
done
PATH="$work/empty-path" "$binary" recover-install --project "$project"
PATH="$work/empty-path" "$binary" uninstall both --project "$project"
cmp "$work/codex-before" "$project/.codex/config.toml"
grep -q 'preserved-server' "$project/.mcp.json"
grep -q '"user": true\|"user":true' "$project/.mcp.json"
if grep -q '"decision_browser"' "$project/.mcp.json"; then
    printf '%s\n' 'Uninstall left the managed server registered.' >&2
    exit 1
fi

printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"archive-smoke","version":"1"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' |
    DECISION_BROWSER_PROVIDER=local DECISION_BROWSER_CONFIG_FILE="$work/absent-profile.json" \
        PATH="$work/empty-path" "$binary" serve --admission standard_form > "$work/discovery.jsonl"
for name in capabilities start_task get_task cancel_task read_artifact present_admission; do
    grep -q "\"name\":\"$name\"" "$work/discovery.jsonl"
done
printf '%s\n' 'Native archive installation, upgrade, recovery preview, uninstall and six-tool discovery passed with an empty executable search path.'
if [ -n "${DECISION_BROWSER_CHROME:-}" ]; then
    "$binary" probe-browser > "$work/probe.json"
    grep -q '"status":"verified"' "$work/probe.json"
    cat "$work/probe.json"
fi

#!/bin/sh
# Check the tracked release tree. Dependencies that spawn another runtime still
# require a clean-machine smoke test after this source check passes.
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

files=$(git ls-files --cached --others --exclude-standard -- '*.py' '*.pyi' '*.pyw' '*.pyz' '*.whl' \
    ':(glob)**/pyproject.toml' ':(glob)**/uv.lock' ':(glob)**/requirements*.txt' ':(glob)**/Pipfile' ':(glob)**/Pipfile.lock' |
    while IFS= read -r path; do
        if [ -e "$path" ]; then printf '%s\n' "$path"; fi
    done)
if [ -n "$files" ]; then
    printf '%s\n' 'Python code or packaging files remain in the release tree:' >&2
    printf '%s\n' "$files" >&2
    exit 1
fi

if git grep -nE 'setup-python|astral-sh/setup-uv|--managed-python|python[0-9.]*[[:space:]]+(-I|-m|-c)|/usr/bin/env[[:space:]]+python' -- .github npm scripts skills ':!scripts/check-python-free.sh'; then
    printf '%s\n' 'Active Python bootstrap or invocation remains.' >&2
    exit 1
else
    result=$?
    [ "$result" -eq 1 ] || exit "$result"
fi

printf '%s\n' 'Release tree has no Python sources, packaging or known bootstrap commands.'

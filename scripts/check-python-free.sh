#!/bin/sh
# Check the tracked release tree. Dependencies that spawn another runtime still
# require a clean-machine smoke test after this source check passes.
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

files=$(git ls-files -- '*.py' '*.pyi' '*.pyw' '*.pyz' '*.whl' 'pyproject.toml' 'uv.lock' 'requirements*.txt' 'Pipfile' 'Pipfile.lock')
if [ -n "$files" ]; then
    printf '%s\n' 'Tracked Python code or packaging files:' >&2
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

printf '%s\n' 'Tracked release tree has no Python sources, packaging or known bootstrap commands.'

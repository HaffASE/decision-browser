"""Check version/lock consistency and explicit release prerequisites without network."""

from __future__ import annotations

import argparse
import json
import re
import runpy
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METADATA = runpy.run_path(str(Path(__file__).with_name("build_metadata.py")))
VERSION = METADATA["VERSION"]


def check(root: Path, tag: str | None = None) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    expected_npm = METADATA["npm_version"](version)
    package = json.loads((root / "npm/package.json").read_text(encoding="utf-8"))
    if package.get("version") != expected_npm:
        raise ValueError("Python and npm launcher versions disagree.")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [p for p in lock["package"] if p["name"] == project["name"]]
    if len(packages) != 1 or packages[0]["version"] != version:
        raise ValueError("Project version and uv.lock disagree; run uv lock.")
    if tag is not None:
        if tag != f"v{version}":
            raise ValueError(f"Tag must equal v{version}.")
        license_text = (
            (root / "LICENSE").read_text(encoding="utf-8") if (root / "LICENSE").is_file() else ""
        )
        if not license_text.strip() or not project.get("license"):
            raise ValueError(
                "Release requires owner-approved LICENSE and project.license metadata."
            )
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        match = re.search(
            rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$",
            changelog,
            re.MULTILINE,
        )
        if match is None:
            raise ValueError(
                "Release requires a dated CHANGELOG entry for this version."
            )
        date.fromisoformat(match.group(1))
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag", help="Require release prerequisites and exact v<version> match."
    )
    args = parser.parse_args()
    try:
        version = check(ROOT, args.tag)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Release check failed: {exc}\n")
    print(f"Version OK: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

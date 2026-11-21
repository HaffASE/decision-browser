"""Prepare an npm launcher directory from a reviewed wheel; never publish it."""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import shutil
import tomllib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
NPM_VERSION = runpy.run_path(str(Path(__file__).with_name("build_metadata.py")))["npm_version"]


def build(wheel: Path, output: Path, root: Path = ROOT) -> None:
    package = json.loads((root / "npm/package.json").read_text(encoding="utf-8"))
    with ZipFile(wheel) as archive:
        prefix = "decision_browser/bootstrap/"
        project = tomllib.loads(archive.read(prefix + "pyproject.toml").decode("utf-8"))["project"]
        if project["name"] != "decision-browser" or NPM_VERSION(project["version"]) != package["version"]:
            raise ValueError("Wheel and npm launcher versions must match")
        notices = {name: archive.read(prefix + name)
                   for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")}
    installer_builder = runpy.run_path(str(root / "scripts/build_installer.py"))
    installer_builder["bootstrap_payload"](wheel)
    # Never overwrite a previously reviewed candidate.
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(root / "npm/bin", output / "bin")
    for name in ("package.json", "README.md"):
        shutil.copyfile(root / "npm" / name, output / name)
    for name, data in notices.items():
        (output / name).write_bytes(data)
    (output / "bin/decision-browser.cjs").chmod(0o755)
    installer = output / "installer"
    installer.mkdir()
    target = installer / "decision-browser.pyz"
    installer_builder["build"](wheel, target)
    (installer / "manifest.json").write_text(json.dumps({
        "file": target.name,
        "version": package["version"],
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.wheel, args.output)


if __name__ == "__main__":
    main()

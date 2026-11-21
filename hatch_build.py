"""Bundle reviewed build inputs so an installed wheel needs no external checkout."""
import os
import runpy
import shutil
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

METADATA = Path(__file__).with_name("scripts") / "build_metadata.py"
if METADATA.is_symlink() or METADATA.parent.is_symlink():
    raise ValueError("Source symlinks are not supported.")
INPUTS = runpy.run_path(str(METADATA))["BOOTSTRAP_INPUTS"]


def reject_symlinks(root):
    # Validate before loading/copying any source input, for sdist and wheel alike.
    for name in INPUTS:
        relative = Path(name)
        if any((root / part).is_symlink() for part in (relative, *relative.parents)):
            raise ValueError("Source symlinks are not supported.")
        if (root / name).is_dir():
            for directory, dirs, files in os.walk(root / name, followlinks=False):
                if any((Path(directory) / child).is_symlink() for child in (*dirs, *files)):
                    raise ValueError("Source symlinks are not supported.")


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        reject_symlinks(root)
        if self.target_name != "wheel":
            return
        self.bootstrap = tempfile.TemporaryDirectory(prefix="decision-browser-wheel-")
        target = Path(self.bootstrap.name)
        for name in INPUTS:
            source, destination = root / name, target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
            else:
                # Preserve a link inserted during copying so validation rejects it.
                shutil.copyfile(source, destination, follow_symlinks=False)
        reject_symlinks(target)
        build_data["force_include"][str(target)] = "decision_browser/bootstrap"

    def finalize(self, version, build_data, artifact_path):
        if self.target_name == "wheel":
            self.bootstrap.cleanup()

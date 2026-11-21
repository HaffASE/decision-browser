"""Build a stdlib-only executable installer archive from the tested wheel."""
from __future__ import annotations

import argparse
import runpy
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

REQUIRED_BOOTSTRAP_FILES = runpy.run_path(
    str(Path(__file__).with_name("build_metadata.py"))
)["REQUIRED_BOOTSTRAP_FILES"]

MAIN = '''import runpy, sys, tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
with tempfile.TemporaryDirectory(prefix="decision-browser-bootstrap-") as directory:
    root = Path(directory)
    with ZipFile(sys.argv[0]) as archive:
        for name in archive.namelist():
            if not name.startswith("payload/"):
                continue
            relative = PurePosixPath(name.removeprefix("payload/"))
            if relative.is_absolute() or ".." in relative.parts or "\\\\" in str(relative) or ":" in str(relative):
                raise SystemExit("Invalid installer archive path")
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    script = "scripts/prepare_tool_runtime.py" if "--prepare-runtime" in sys.argv[1:] else "scripts/install.py"
    runpy.run_path(str(root / script), run_name="__main__")
'''


def bootstrap_payload(wheel: Path) -> dict[str, bytes]:
    """Validate all consumed inputs before either builder creates a candidate."""
    with ZipFile(wheel) as source:
        prefix = "decision_browser/bootstrap/"
        payload = {name[len(prefix):]: source.read(name) for name in source.namelist()
                   if name.startswith(prefix) and not name.endswith("/")}
    if not set(REQUIRED_BOOTSTRAP_FILES) <= payload.keys():
        raise ValueError("Wheel lacks complete bootstrap inputs")
    for name in payload:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("Invalid bootstrap path")
    return payload


def build(wheel: Path, output: Path) -> None:
    # Exclusive creation protects previously reviewed distribution artifacts.
    payload = bootstrap_payload(wheel)
    with output.open("xb") as handle, ZipFile(handle, "w", ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", MAIN)
        archive.writestr("payload/prebuilt/" + wheel.name, wheel.read_bytes())
        for name, data in sorted(payload.items()):
            archive.writestr("payload/" + name, data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.wheel, args.output)


if __name__ == "__main__":
    main()

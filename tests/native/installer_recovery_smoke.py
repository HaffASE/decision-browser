"""Create a real blocked first install and exercise a packed launcher on this boot."""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@contextmanager
def untrusted_mirror():
    # A leaf issued by an unknown CA, valid for the fixture address. Keep both
    # the key and the CA outside the test project and outside system trust.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        def openssl(*args: str) -> None:
            subprocess.run(["openssl", *args], cwd=root, capture_output=True, check=True, timeout=30)

        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
                "-out", "ca.crt", "-subj", "/CN=Installer smoke CA", "-days", "1")
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "tls.key",
                "-out", "tls.csr", "-subj", "/CN=localhost")
        (root / "ext").write_text("basicConstraints=CA:FALSE\nsubjectAltName=IP:127.0.0.1\n")
        openssl("x509", "-req", "-in", "tls.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                "-CAcreateserial", "-out", "tls.crt", "-extfile", "ext", "-days", "1")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root / "tls.crt", root / "tls.key")
        with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"https://127.0.0.1:{server.server_port}/simple"
            finally:
                server.shutdown()
                thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="reviewed tarball with recovery commands")
    parser.add_argument("--installer-package", type=Path, help="optional older tarball to generate the failure")
    parser.add_argument("--work-dir", type=Path, required=True, help="new private directory on a local volume")
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    installer = args.installer_package.resolve(strict=True) if args.installer_package else package
    work = args.work_dir.resolve()
    work.mkdir(mode=0o700, exist_ok=False)
    project = work / "project"
    project.mkdir(mode=0o700)
    # Reuse locally installed tools, including uv-managed Python. Do not inherit
    # provider credentials, proxies, CA overrides, or launcher configuration.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
    env["npm_config_cache"] = str(work / "npm-cache")

    def run(tarball: Path, args: list[str], **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["npm", "exec", "--offline", "--yes", "--package=" + str(tarball), "--", "decision-browser", *args],
            env={**env, **overrides}, cwd=project, capture_output=True, text=True, timeout=180, check=False,
        )

    with untrusted_mirror() as mirror:
        failed = run(installer, ["install", "codex"], UV_SYSTEM_CERTS="true", DECISION_BROWSER_INSTALL_INDEX_URL=mirror)
    stages = [json.loads(line) for line in failed.stderr.splitlines() if line.startswith("{")]
    assert failed.returncode == 1 and any(r.get("stage") == "locked Python package" and
        r.get("cleanup") == "unknown_retained" and r.get("hint") in {"tls_unknown_issuer", "tls_certificate"}
        for r in stages), "Expected a TLS failure during locked package preparation; check the prerequisites"
    lease = project / ".decision-browser-install.lease"
    residual = project / ".decision-browser-stage-cleanup-unknown.json"
    original = (lease.stat().st_ino, lease.read_bytes(), residual.read_bytes())
    for argv, expected in [
        (["install-status"], "record_reboot_witness"),
        (["recover-install"], "record_reboot_witness"),
        (["recover-install", "--apply"], "reboot_required"),
        (["recover-install", "--apply"], "reboot_required"),
    ]:
        result = run(package, [*argv, "--json"])
        report = json.loads(result.stdout)
        print(" ".join(argv), report, flush=True)
        assert result.returncode == 1 and report["status"] == "blocked" and report["recovery"] == expected
        assert lease.stat().st_ino == original[0] and lease.read_bytes() == original[1]
        assert residual.read_bytes() == original[2]
        assert not (project / ".codex/config.toml").exists()
        assert not (project / ".mcp.json").exists()
        assert not (project / ".decision-browser-install/manifest.json").exists()
    print(f"Retained test project: {project}")


if __name__ == "__main__":
    main()

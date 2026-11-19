"""Image hardening: the build step that replaces the Cursor SDK's vulnerable undici
(docker/patch_cursor_bridge.py), and what the Dockerfile must keep doing.
No network: the "download" is a small package built in the test."""
import base64
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR / "docker"))
import patch_cursor_bridge as pcb  # noqa: E402

POLYFILL = 'import { Headers as HeadersPolyfill } from "undici";\nif (major < 18) { globalThis.Headers = HeadersPolyfill; }\n'


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def package(node_modules: Path, name: str, version: str, deps=None):
    write(node_modules / name / "package.json",
          json.dumps({"name": name, "version": version, "dependencies": deps or {}}))


@pytest.fixture
def bridge(tmp_path):
    """The layout cursor-sdk 1.3.1 ships: undici 5 (needs @fastify/busboy), used only by connect-node."""
    nm = tmp_path / "bridge" / "node_modules"
    package(nm, "undici", "5.29.0", {"@fastify/busboy": "^2.0.0"})
    write(nm / "undici" / "lib" / "fetch" / "body.js", "const Busboy = require('@fastify/busboy')\n")
    package(nm, "@fastify/busboy", "2.1.1")
    package(nm, "@connectrpc/connect-node", "1.6.1", {"undici": "^5.28.4"})
    write(nm / "@connectrpc" / "connect-node" / "dist" / "esm" / "node-headers-polyfill.js", POLYFILL)
    write(nm / "@connectrpc" / "connect-node" / "dist" / "cjs" / "node-headers-polyfill.js",
          'const undici_1 = require("undici");\n')
    package(nm, "@cursor/sdk", "1.3.1")
    write(nm / "@cursor" / "sdk" / "dist" / "index.js", "export const x = 1;\n")
    write(tmp_path / "bridge" / "dist" / "index.js", 'import { x } from "@cursor/sdk";\n')
    return tmp_path / "bridge"


def tarball(version=pcb.UNDICI_VERSION, name="undici", deps=None, extra=()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        files = {"package/package.json": json.dumps({"name": name, "version": version, "dependencies": deps or {}}),
                 "package/index.js": "module.exports.Headers = class Headers {}\n"}
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info, data in extra:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


@pytest.fixture
def download(monkeypatch):
    """Serve a fake undici package whose checksum is the pinned one; record the URLs asked for."""
    data, urls = tarball(), []
    monkeypatch.setattr(pcb, "UNDICI_INTEGRITY", integrity(data))

    def fetch(url):
        urls.append(url)
        return data
    fetch.urls = urls
    return fetch


def undici_version(bridge: Path) -> str:
    return pcb.package_json(bridge / "node_modules" / "undici")["version"]


def test_replaces_undici_and_removes_busboy(bridge, download):
    checked = []
    result = pcb.patch(bridge, fetch=download, check=checked.append)
    assert result == (f"replaced undici 5.29.0 with {pcb.UNDICI_VERSION}; "
                      "removed @fastify/busboy (only undici 5 used it)")
    assert undici_version(bridge) == pcb.UNDICI_VERSION
    assert not (bridge / "node_modules" / "@fastify").exists()
    assert not (bridge / "node_modules" / "undici" / "lib").exists()  # nothing of the old version is left
    assert not (bridge / "node_modules" / ".undici-new").exists()
    assert checked == [bridge]  # the bridge's modules were loaded afterwards
    assert download.urls == [f"https://registry.npmjs.org/undici/-/undici-{pcb.UNDICI_VERSION}.tgz"]


def test_second_run_does_nothing(bridge, download):
    pcb.patch(bridge, fetch=download, check=lambda b: None)
    assert pcb.patch(bridge, fetch=download, check=lambda b: None) == (
        f"nothing to do: the Cursor SDK already bundles undici {pcb.UNDICI_VERSION}")
    assert len(download.urls) == 1


def test_sdk_without_undici_needs_nothing(bridge, download):
    import shutil
    shutil.rmtree(bridge / "node_modules" / "undici")
    assert pcb.patch(bridge, fetch=download).startswith("nothing to do")
    assert download.urls == []


def test_wrong_checksum_installs_nothing(bridge, download, monkeypatch):
    monkeypatch.setattr(pcb, "UNDICI_INTEGRITY", integrity(b"something else"))
    with pytest.raises(pcb.PatchError, match="does not match the pinned SHA-512"):
        pcb.patch(bridge, fetch=download)
    assert undici_version(bridge) == "5.29.0" and (bridge / "node_modules" / "@fastify" / "busboy").is_dir()


def test_unknown_importer_stops_the_build_before_any_download(bridge, download):
    write(bridge / "node_modules" / "@cursor" / "sdk" / "dist" / "http.js", 'const { Agent } = require("undici");\n')
    with pytest.raises(pcb.PatchError, match=r"@cursor/sdk/dist/http\.js"):
        pcb.patch(bridge, fetch=download)
    assert download.urls == [] and undici_version(bridge) == "5.29.0"


def test_bridge_code_importing_undici_also_stops_the_build(bridge, download):
    write(bridge / "dist" / "fetch.js", 'import { fetch } from "undici";\n')
    with pytest.raises(pcb.PatchError, match=r"dist/fetch\.js"):
        pcb.patch(bridge, fetch=download)


def test_busboy_stays_when_something_else_needs_it(bridge, download):
    package(bridge / "node_modules", "other", "1.0.0", {"@fastify/busboy": "^2.0.0"})
    result = pcb.patch(bridge, fetch=download, check=lambda b: None)
    assert "removed" not in result and (bridge / "node_modules" / "@fastify" / "busboy").is_dir()


def test_failed_module_check_fails_the_patch(bridge, download):
    def broken(_bridge):
        raise pcb.PatchError("the bridge's modules no longer load: boom")
    with pytest.raises(pcb.PatchError, match="no longer load"):
        pcb.patch(bridge, fetch=download, check=broken)


def fake_node(bridge, monkeypatch, results):
    """A bundled 'node' whose runs return the given exit codes, in order."""
    write(bridge / "bin" / "node", "")
    codes = list(results)

    def run(cmd, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=codes.pop(0), stdout="bridge modules load", stderr="Cannot find module x")
    monkeypatch.setattr(pcb.subprocess, "run", run)
    return codes


def test_module_check_passes_and_fails_on_the_real_signal(bridge, monkeypatch, capsys):
    fake_node(bridge, monkeypatch, [0, 0])
    pcb.smoke_test(bridge)
    assert "bridge modules load" in capsys.readouterr().out
    fake_node(bridge, monkeypatch, [0, 1])  # Node runs, but the modules don't load: a real failure
    with pytest.raises(pcb.PatchError, match="no longer load: Cannot find module x"):
        pcb.smoke_test(bridge)


def test_module_check_is_skipped_when_node_cannot_run_here(bridge, monkeypatch, capsys):
    left = fake_node(bridge, monkeypatch, [1, 0])  # e.g. an ARM build under emulation
    pcb.smoke_test(bridge)
    assert "module-load check skipped" in capsys.readouterr().out and left == [0]  # the second run never happened


def test_missing_node_is_an_error(bridge):
    with pytest.raises(pcb.PatchError, match="bundled Node.js was not found"):
        pcb.smoke_test(bridge)


def test_download_must_be_the_pinned_package(bridge, monkeypatch):
    for data, message in [(tarball(version="6.0.0"), "unexpected package"),
                          (tarball(name="evil"), "unexpected package"),
                          (tarball(deps={"left-pad": "1"}), "has dependencies")]:
        monkeypatch.setattr(pcb, "UNDICI_INTEGRITY", integrity(data))
        with pytest.raises(pcb.PatchError, match=message):
            pcb.patch(bridge, fetch=lambda url, d=data: d)
        assert undici_version(bridge) == "5.29.0"  # the old one is only removed after every check


def test_archive_entries_outside_the_package_are_ignored(tmp_path):
    link = tarfile.TarInfo("package/link")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    outside = tarfile.TarInfo("package/../../escape.js")
    outside.size = 1
    other_root = tarfile.TarInfo("elsewhere/file.js")
    other_root.size = 1
    pcb.extract(tarball(extra=[(link, None), (outside, b"x"), (other_root, b"y")]), tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").rglob("*")) == ["index.js", "package.json"]
    assert not (tmp_path / "escape.js").exists()


def test_main_reports_failure_with_exit_code(bridge, monkeypatch, capsys):
    monkeypatch.setattr(pcb, "download", lambda url: b"not the pinned file")
    assert pcb.main(["--bridge-dir", str(bridge)]) == 1
    assert "ERROR:" in capsys.readouterr().err


def test_pin_is_a_fixed_release_with_a_real_checksum():
    assert pcb.version_tuple(pcb.UNDICI_VERSION) >= (6, 29, 1)  # first 6.x with every November 2026 fix
    algorithm, _, digest = pcb.UNDICI_INTEGRITY.partition("-")
    assert algorithm == "sha512" and len(base64.b64decode(digest)) == 64


def test_version_comparison():
    assert pcb.version_tuple("5.29.0") < pcb.version_tuple("6.29.1") < pcb.version_tuple("6.30.0")
    assert pcb.version_tuple("7.0.0-alpha.10") == (7, 0, 0)


# --- Dockerfile ---

DOCKERFILE = (AGENT_DIR / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_applies_os_security_updates():
    assert re.search(r"apt-get update\s*\\\s*&& .*apt-get upgrade -y", DOCKERFILE)
    assert "rm -rf /var/lib/apt/lists/*" in DOCKERFILE  # no package lists left in the image


def test_dockerfile_patches_the_cursor_sdk_only_when_it_is_installed():
    branch = DOCKERFILE[DOCKERFILE.index('if [ "$WITH_CURSOR" = "true" ]'):DOCKERFILE.index("fi \\")]
    assert "requirements-cursor.txt" in branch and "python /opt/build/patch_cursor_bridge.py" in branch
    assert "&& python /opt/build/patch_cursor_bridge.py" in branch  # a failed patch fails the build


def test_dockerfile_leaves_no_pip_and_no_build_helpers():
    assert "pip install --no-cache-dir --upgrade pip" in DOCKERFILE
    assert DOCKERFILE.index("pip uninstall -y pip") > DOCKERFILE.index("patch_cursor_bridge.py;")
    assert "'ensurepip', '_bundled'" in DOCKERFILE
    assert "rm -rf /opt/build" in DOCKERFILE and "rm -rf /app/docker" in DOCKERFILE
    assert DOCKERFILE.index("USER 10001") > DOCKERFILE.index("rm -rf /app/docker")  # runs as non-root

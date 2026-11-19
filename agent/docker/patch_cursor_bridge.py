"""Image build step: replace the vulnerable `undici` bundled in the Cursor SDK.

Run by the Dockerfile right after `pip install cursor-sdk`, not used at runtime.

Background: cursor-sdk bundles a helper program (the "bridge") with its own Node.js and
node_modules. Up to 1.3.1 that includes undici 5.29.0, which pulls in @fastify/busboy 2.1.1.
The undici 5.x line doesn't get security fixes any more, so image scanners flag it (13 CVEs
for undici and 2 for busboy in Nov 2026). undici is only there because
@connectrpc/connect-node imports its Headers class as a fallback for Node < 18. The bridge
runs on its bundled Node 22, so that fallback is never used.

Steps:
  1. find the bridge's node_modules inside the installed cursor_sdk package
  2. nothing to do if undici is missing or already at least UNDICI_VERSION
  3. bail out if anything besides the known Headers fallback imports undici, a major
     version swap would need a proper look then
  4. download the pinned undici from the npm registry and check its SHA-512
  5. replace node_modules/undici, and remove @fastify/busboy if nothing needs it
  6. load the bridge's modules with the bundled Node to make sure they still import
Any failure fails the image build. We never want a half-patched image.

Bumping undici: set UNDICI_VERSION and UNDICI_INTEGRITY ("dist.integrity" from
https://registry.npmjs.org/undici/<version>) and rebuild. Stay on a release line whose
"engines" the bundled Node satisfies. Once cursor-sdk ships a fixed undici this script only
reports "nothing to do" and the build step can go.
"""
import argparse
import base64
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

UNDICI_VERSION = "6.29.1"
UNDICI_INTEGRITY = "sha512-EvRlUt/QqAEvXdQeRMYUfwOQsfzpDztykrG2owGEE/rBOBoYv9uF39ZzsNDh4lTFHhTr2XOZjKkYgueD12ZmGA=="
TARBALL_URL = "https://registry.npmjs.org/undici/-/undici-{version}.tgz"

# the one place that may import undici: connect-node's Headers fallback for old Node
KNOWN_IMPORTERS = re.compile(r"^@connectrpc/connect-node/dist/(esm|cjs)/node-headers-polyfill\.js$")
IMPORTS = re.compile(r"""(?:require\(\s*|from\s+|import\(\s*|import\s+)["']({name})(?:/[^"']*)?["']""")
SMOKE_TEST = ("const u = await import('undici');"
              "if (typeof u.Headers !== 'function') { console.error('undici has no Headers export'); process.exit(1); }"
              "await import('@connectrpc/connect-node');"
              "console.log('bridge modules load with undici ' + (await import('node:module')).createRequire("
              "import.meta.url)('undici/package.json').version);")


class PatchError(RuntimeError):
    pass


def version_tuple(version: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-")[0])[:3])


def find_bridge() -> Path:
    import importlib.util
    spec = importlib.util.find_spec("cursor_sdk")
    if spec is None or not spec.origin:
        raise PatchError("cursor_sdk is not installed")
    return Path(spec.origin).parent / "_vendor" / "bridge"


def package_json(path: Path) -> dict:
    return json.loads((path / "package.json").read_text(encoding="utf-8"))


def packages(node_modules: Path):
    """Yields (name, directory) for every top-level package, scoped ones too."""
    for entry in sorted(node_modules.iterdir()):
        if entry.name.startswith("@") and entry.is_dir():
            for scoped in sorted(entry.iterdir()):
                if (scoped / "package.json").is_file():
                    yield f"{entry.name}/{scoped.name}", scoped
        elif (entry / "package.json").is_file():
            yield entry.name, entry


def importers(bridge: Path, name: str) -> list[str]:
    """Files outside the package itself that import `name` (paths relative to node_modules or dist/)."""
    pattern = re.compile(IMPORTS.pattern.format(name=re.escape(name)))
    node_modules = bridge / "node_modules"
    found = []
    for root in (node_modules, bridge / "dist"):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in (".js", ".mjs", ".cjs") or not path.is_file():
                continue
            relative = path.relative_to(node_modules if root == node_modules else bridge).as_posix()
            if relative.startswith(f"{name}/"):
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                found.append(relative)
    return found


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "kubesupport-image-build"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def verify(data: bytes, integrity: str):
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512":
        raise PatchError(f"unsupported integrity algorithm '{algorithm}'")
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != expected:
        raise PatchError("the downloaded undici package does not match the pinned SHA-512; not installing it")


def extract(data: bytes, target: Path):
    """Unpack an npm tarball into target. Everything in it sits under "package/". Files only."""
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if not member.isfile() or len(parts) < 2 or parts[0] != "package" or ".." in parts:
                continue  # no links, devices or paths outside the package
            destination = target.joinpath(*parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, open(destination, "wb") as out:
                shutil.copyfileobj(source, out)


def needed_by(node_modules: Path, name: str) -> list[str]:
    users = []
    for package, path in packages(node_modules):
        meta = package_json(path)
        if name in {**meta.get("dependencies", {}), **meta.get("peerDependencies", {})}:
            users.append(package)
    return users


def smoke_test(bridge: Path):
    """Load the bridge's modules with its own Node. If that Node can't run here at all (a build
    for another CPU under emulation) we say so and carry on, the build for the native CPU runs
    the same check."""
    node = next((p for p in (bridge / "bin" / "node", bridge / "bin" / "node.exe") if p.is_file()), None)
    if node is None:
        raise PatchError("the bridge's bundled Node.js was not found, so the patch can't be checked")
    try:
        runs = subprocess.run([str(node), "-e", "process.exit(0)"], cwd=bridge, capture_output=True,
                              timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        runs = False
    if not runs:
        print("WARNING: the bundled Node.js can't run in this build environment; module-load check skipped")
        return
    result = subprocess.run([str(node), "--input-type=module", "-e", SMOKE_TEST], cwd=bridge, capture_output=True,
                            text=True, timeout=180)
    if result.returncode != 0:
        raise PatchError(f"the bridge's modules no longer load: {(result.stderr or result.stdout).strip()[:500]}")
    print(result.stdout.strip())


def patch(bridge: Path, fetch=download, check=smoke_test) -> str:
    node_modules = bridge / "node_modules"
    undici = node_modules / "undici"
    if not (undici / "package.json").is_file():
        return "nothing to do: the Cursor SDK no longer bundles undici"
    current = package_json(undici).get("version", "0")
    if version_tuple(current) >= version_tuple(UNDICI_VERSION):
        return f"nothing to do: the Cursor SDK already bundles undici {current}"

    unexpected = [f for f in importers(bridge, "undici") if not KNOWN_IMPORTERS.match(f)]
    if unexpected:
        raise PatchError("undici is imported by code this patch doesn't know: " + ", ".join(unexpected[:5])
                         + ". Review whether undici " + UNDICI_VERSION + " is compatible before changing this script.")

    data = fetch(TARBALL_URL.format(version=UNDICI_VERSION))
    verify(data, UNDICI_INTEGRITY)
    staging = node_modules / ".undici-new"
    shutil.rmtree(staging, ignore_errors=True)
    extract(data, staging)
    new = package_json(staging)
    if new.get("name") != "undici" or new.get("version") != UNDICI_VERSION:
        raise PatchError(f"unexpected package in the download: {new.get('name')} {new.get('version')}")
    if new.get("dependencies"):
        raise PatchError(f"undici {UNDICI_VERSION} has dependencies this patch would not install: {new['dependencies']}")
    shutil.rmtree(undici)
    staging.rename(undici)

    removed = ""
    busboy = node_modules / "@fastify" / "busboy"
    if busboy.is_dir() and not needed_by(node_modules, "@fastify/busboy") and not importers(bridge, "@fastify/busboy"):
        shutil.rmtree(busboy)
        if not any(busboy.parent.iterdir()):
            busboy.parent.rmdir()
        removed = "; removed @fastify/busboy (only undici 5 used it)"
    check(bridge)
    return f"replaced undici {current} with {UNDICI_VERSION}{removed}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bridge-dir", type=Path, help="the bridge folder (default: inside the installed cursor_sdk)")
    args = parser.parse_args(argv)
    try:
        print("Cursor SDK bridge:", patch(args.bridge_dir or find_bridge()))
    except PatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

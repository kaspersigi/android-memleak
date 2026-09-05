#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build upstream BCC memleak for Android arm64, without an AOSP checkout."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "sources.lock"
SOURCES = ROOT / "sources"
BUILD = ROOT / "build"
CACHE = ROOT / ".cache/android-memleak"
OUT = ROOT / "dist"
TAG = re.compile(r"v\d+\.\d+\.\d+\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BuildError(Exception):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def signature(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        stream.write((json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
    try:
        tmp.chmod(0o644)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def run(args, *, cwd=ROOT, capture=False, env=None):
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=False,
        text=True,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        details = (result.stdout or "") + (result.stderr or "") if capture else ""
        raise BuildError(f"Command failed ({result.returncode}): {args[0]}\n{details}")
    return result.stdout if capture else None


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise BuildError("Refusing an HTTPS-to-HTTP download redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


def request(url: str):
    headers = {"User-Agent": "android-memleak-builder/1"}
    # Never send the API token to archives, redirects, or other hosts.
    if url.startswith("https://api.github.com/"):
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(SafeRedirect())
    return opener.open(urllib.request.Request(url, headers=headers), timeout=60)


def api(path: str):
    with request("https://api.github.com/" + path) as response:
        return json.load(response)


def validate_config(config):
    if not isinstance(config, dict):
        raise BuildError("sources.lock must contain a JSON object")
    version = config.get("bcc")
    if not isinstance(version, str) or (
        version != "latest" and not TAG.fullmatch(version)
    ):
        raise BuildError("bcc must be 'latest' or a release tag such as v0.37.0")
    api_level = config.get("android_api")
    if type(api_level) is not int or not 28 <= api_level <= 99:
        raise BuildError("android_api must be an integer >= 28 supported by your NDK")
    for name in ("libbpf", "zstd"):
        entry = config.get(name, {})
        if not isinstance(entry, dict) or not TAG.fullmatch(
            str(entry.get("version", ""))
        ):
            raise BuildError(f"Invalid {name} version")
        if not COMMIT.fullmatch(str(entry.get("commit", ""))):
            raise BuildError(f"{name} requires a pinned 40-character commit")
    elf = config.get("elfutils", {})
    if not isinstance(elf, dict) or not re.fullmatch(
        r"0\.\d+", str(elf.get("version", ""))
    ):
        raise BuildError("Invalid elfutils version")
    if not SHA256.fullmatch(str(elf.get("sha256", ""))):
        raise BuildError("elfutils requires a pinned SHA256")


def resolve_sources(config, offline=False):
    lock = CACHE / ("resolution-" + signature(config) + ".json")
    if offline:
        if not lock.is_file():
            raise BuildError(
                "No cached resolution for this configuration; run online first"
            )
        return json.loads(lock.read_text())
    requested = config["bcc"]
    release = api(
        "repos/iovisor/bcc/releases/"
        + ("latest" if requested == "latest" else "tags/" + requested)
    )
    tag = release["tag_name"]
    if release.get("draft") or release.get("prerelease") or not TAG.fullmatch(tag):
        raise BuildError(f"Not a stable BCC release: {tag}")
    if requested != "latest" and tag != requested:
        raise BuildError("GitHub returned an unexpected BCC release")
    ref = api("repos/iovisor/bcc/git/ref/tags/" + tag)["object"]
    for _ in range(5):
        if ref["type"] == "commit":
            break
        if ref["type"] != "tag" or not COMMIT.fullmatch(ref["sha"]):
            raise BuildError("Unexpected BCC tag target")
        ref = api("repos/iovisor/bcc/git/tags/" + ref["sha"])["object"]
    if ref["type"] != "commit" or not COMMIT.fullmatch(ref["sha"]):
        raise BuildError("Cannot resolve BCC tag to an immutable commit")
    sources = {}
    for name, repo, entry in (
        ("bcc", "iovisor/bcc", {"version": tag, "commit": ref["sha"]}),
        ("libbpf", "libbpf/libbpf", config["libbpf"]),
        ("zstd", "facebook/zstd", config["zstd"]),
    ):
        sources[name] = dict(
            entry, url=f"https://codeload.github.com/{repo}/tar.gz/{entry['commit']}"
        )
    elf = config["elfutils"]
    sources["elfutils"] = dict(
        elf,
        url=f"https://sourceware.org/elfutils/ftp/{elf['version']}/elfutils-{elf['version']}.tar.bz2",
    )
    write_json(lock, sources)
    return sources


def download(source, offline=False):
    url = source["url"]
    key = signature(url)
    archive = CACHE / (key + ".tar")
    receipt = CACHE / (key + ".json")
    if archive.is_file() and receipt.is_file():
        actual = digest(archive)
        expected = source.get("sha256") or json.loads(receipt.read_text())["sha256"]
        if actual != expected:
            raise BuildError(
                f"Corrupt cached archive: {archive}; move it aside and retry"
            )
        return archive, actual
    if offline:
        raise BuildError(f"Archive is not cached: {url}")
    print(f"  Download {url}", flush=True)
    for attempt in range(3):
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as stream:
                tmp = Path(stream.name)
                with request(url) as response:
                    shutil.copyfileobj(response, stream)
            actual = digest(tmp)
            if source.get("sha256") and actual != source["sha256"]:
                raise BuildError(f"SHA256 mismatch: {url}")
            tmp.chmod(0o644)
            tmp.replace(archive)
            write_json(receipt, {"url": url, "sha256": actual})
            return archive, actual
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def extract(archive: Path, destination: Path):
    # Python's data filter prevents absolute paths, traversal and unsafe links.
    # Keep relative in-tree symlinks (BCC's arm64/vmlinux.h is one).
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise BuildError("Expected one top-level source directory in archive")
    return children[0]


def patches(component="bcc"):
    directory = ROOT / "patches" / component
    if not directory.exists() and component != "bcc":
        return []
    result = []
    for line in (directory / "series").read_text().splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if not re.fullmatch(r"[\w.-]+\.patch", name) or name in [
            p.name for p in result
        ]:
            raise BuildError(f"Invalid/duplicate patch in series: {name}")
        path = directory / name
        if not path.is_file():
            raise BuildError(f"Missing patch: {path}")
        result.append(path)
    if not result and component == "bcc":
        raise BuildError("Android patch series must not be empty")
    return result


def tree_digest(directory: Path):
    entries = []
    for path in sorted(directory.rglob("*")):
        if path.name == ".prepared.json":
            continue
        relative = str(path.relative_to(directory))
        if path.is_symlink():
            entries.append((relative, "link", os.readlink(path)))
        elif path.is_file():
            entries.append((relative, "file", digest(path)))
    return signature(entries)


def prepare(name, source, patch_list, offline=False):
    archive, sha = download(source, offline)
    patch_info = [{"name": p.name, "sha256": digest(p)} for p in patch_list]
    key = signature({"archive": sha, "patches": patch_info, "format": 2})
    parent = SOURCES
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / f"{name}-{key[:20]}"
    marker = destination / ".prepared.json"
    if marker.is_file():
        if json.loads(marker.read_text())["tree_sha256"] != tree_digest(destination):
            raise BuildError(
                f"Prepared sources were modified: {destination}. Put changes in patches instead."
            )
    else:
        with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=parent) as tmp:
            tree = extract(archive, Path(tmp))
            for patch in patch_list:
                print(f"  Apply {patch.name}", flush=True)
                # git apply is strict (no fuzz, no automatic skipping/reverse).
                # Source archives are outside any .git repository; --unsafe-paths
                # is deliberately NOT used.
                apply_patch(tree, patch)
            write_json(tree / ".prepared.json", {"tree_sha256": tree_digest(tree)})
            tree.rename(destination)
    return destination, dict(source, archive_sha256=sha, patches=patch_info)


def apply_patch(tree: Path, patch: Path):
    # A source cache nested in our repository must NOT inherit the parent Git
    # worktree: otherwise git apply may silently skip every path in the patch.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env["GIT_CEILING_DIRECTORIES"] = str(tree.parent.resolve())
    before = tree_digest(tree)
    run(["git", "apply", "--check", str(patch)], cwd=tree, env=env)
    run(["git", "apply", "--verbose", str(patch)], cwd=tree, env=env)
    if before == tree_digest(tree):
        raise BuildError(f"Patch did not change any file: {patch}")


def ndk_path(value):
    value = (
        value
        or os.environ.get("ANDROID_NDK_HOME")
        or os.environ.get("ANDROID_NDK_ROOT")
    )
    if not value:
        raise BuildError(
            "Set ANDROID_NDK_HOME or pass --ndk /path/to/android-ndk-r27d (or newer)"
        )
    ndk = Path(value).expanduser().resolve()
    if not (ndk / "build/cmake/android.toolchain.cmake").is_file():
        raise BuildError(f"Invalid NDK: {ndk}")
    return ndk


def verify_elf(path: Path):
    data = path.read_bytes()
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        raise BuildError("Artifact must be a little-endian ELF64")
    elf_type, machine = struct.unpack_from("<HH", data, 16)
    if machine != 183 or elf_type != 2:
        raise BuildError("Artifact must be a static ARM64 ET_EXEC executable")
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    if size != 56 or not count or offset + size * count > len(data):
        raise BuildError("Invalid ELF program header table")
    loads = 0
    for i in range(count):
        header = struct.unpack_from("<IIQQQQQQ", data, offset + i * size)
        kind, _, file_offset, address, _, filesz, _, alignment = header
        if kind in (2, 3):
            raise BuildError("Artifact has PT_DYNAMIC/PT_INTERP: not fully static")
        if kind == 1:
            loads += 1
            if alignment < 16384 or (address - file_offset) % 16384:
                raise BuildError("Artifact is not 16 KiB page aligned")
            if file_offset + filesz > len(data):
                raise BuildError("Truncated ELF load segment")
    if not loads:
        raise BuildError("Artifact has no load segments")
    return {
        "architecture": "aarch64",
        "static": True,
        "page_alignment": 16384,
        "bytes": len(data),
        "sha256": digest(path),
    }


def build(args, config, sources, provenance):
    ndk = ndk_path(args.ndk)
    tools = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    bpftool = shutil.which(args.bpftool)
    if not bpftool:
        raise BuildError(
            "Host bpftool is required: install bpftool or pass --bpftool PATH"
        )
    for tool in ("cmake", "ninja"):
        if not shutil.which(tool):
            raise BuildError(f"Missing host tool: {tool}")
    toolchain = {
        "ndk": (ndk / "source.properties").read_text().strip(),
        "clang": run([tools / "clang", "--version"], capture=True).strip(),
        "bpftool": run([bpftool, "version"], capture=True).strip(),
        "android_api": config["android_api"],
    }
    key = signature(
        {
            "sources": provenance,
            "ndk_path": str(ndk),
            "tools": toolchain,
            "root": str(ROOT),
            "rules": {
                "CMakeLists.txt": digest(ROOT / "CMakeLists.txt"),
                "cmake": tree_digest(ROOT / "cmake"),
                "compat": tree_digest(ROOT / "compat"),
                "sources.lock": signature(config),
            },
            "tests": tree_digest(ROOT / "tests/native"),
            "script": digest(Path(__file__)),
        }
    )
    output = BUILD / "android-arm64" / key[:20]
    args_cmake = [
        "cmake",
        "-S",
        ROOT,
        "-B",
        output,
        "-G",
        "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={ndk}/build/cmake/android.toolchain.cmake",
        "-DANDROID_ABI=arm64-v8a",
        f"-DANDROID_PLATFORM=android-{config['android_api']}",
        "-DANDROID_STL=c++_static",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.10",
        f"-DBPFTOOL={bpftool}",
        f"-DELFUTILS_VERSION={config['elfutils']['version']}",
    ]
    args_cmake += [f"-D{name.upper()}_SOURCE={path}" for name, path in sources.items()]
    # CMake 4 policy compatibility for the unmodified NDK r27 toolchain and
    # its nested try_compile projects. The environment reaches nested CMake.
    cmake_env = dict(os.environ, CMAKE_POLICY_VERSION_MINIMUM="3.10")
    run(args_cmake, env=cmake_env)
    run(["cmake", "--build", output, "--target", "memleak", "--parallel", args.jobs])
    verify_elf(output / "memleak")
    if args.self_test:
        qemu = shutil.which("qemu-aarch64")
        if not qemu:
            raise BuildError(
                "--self-test requires qemu-aarch64 (Ubuntu package qemu-user)"
            )
        run(
            [
                "cmake",
                "--build",
                output,
                "--target",
                "memleak-options-test",
                "--parallel",
                args.jobs,
            ]
        )
        run(
            [
                sys.executable,
                ROOT / "tests/native/smoke.py",
                qemu,
                output / "memleak-options-test",
                output / "memleak",
            ]
        )
    # Publish only after successful build + inspection. Previous good output is
    # left intact on download, patch, compiler, or verification failure.
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".memleak-", dir=OUT) as tmp:
        stage = Path(tmp)
        shutil.copy2(output / "memleak", stage / "memleak")
        run([tools / "llvm-strip", "--strip-unneeded", stage / "memleak"])
        (stage / "memleak").chmod(0o755)
        details = verify_elf(stage / "memleak")
        write_json(
            stage / "build-info.json",
            {
                "project": "android-memleak",
                "sources": provenance,
                "toolchain": toolchain,
                "artifact": details,
                "recipe_sha256": key,
            },
        )
        (stage / "SHA256SUMS").write_text(f"{details['sha256']}  memleak\n")
        for name in ("memleak", "build-info.json", "SHA256SUMS"):
            (stage / name).replace(OUT / name)
    print(
        f"\nSuccess: {OUT / 'memleak'}\nBCC {provenance['bcc']['version']}; "
        f"static Android ARM64; {details['bytes']} bytes; 16 KiB aligned",
        flush=True,
    )


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--config",
        type=Path,
        default=CONFIG,
        help="JSON source manifest (sources.lock)",
    )
    p.add_argument("--version", help="override BCC release (v0.37.0 or latest)")
    p.add_argument("--ndk", help="Android NDK directory; default ANDROID_NDK_HOME/ROOT")
    p.add_argument("--bpftool", default="bpftool", help="host bpftool executable")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    p.add_argument(
        "--offline", action="store_true", help="use previously verified cached sources"
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="run userspace-only Android parser tests using qemu-aarch64",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-only",
        action="store_true",
        help="fetch sources and apply patches only",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="inspect the published ELF and checksum",
    )
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.jobs < 1:
        raise BuildError("--jobs must be positive")
    if args.verify_only:
        actual = verify_elf(OUT / "memleak")
        metadata = json.loads((OUT / "build-info.json").read_text())
        if (
            actual != metadata["artifact"]
            or (OUT / "SHA256SUMS").read_text() != f"{actual['sha256']}  memleak\n"
        ):
            raise BuildError(
                "Published artifact does not match its provenance/checksum"
            )
        print(json.dumps(actual, indent=2))
        return 0
    if sys.version_info < (3, 12):
        raise BuildError("Python 3.12+ is required for safe archive extraction")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise BuildError("Currently supported build host: Linux x86_64")
    if not shutil.which("git"):
        raise BuildError("git is required to apply patches")
    config = json.loads(args.config.read_text())
    if args.version:
        config["bcc"] = args.version
    validate_config(config)
    if not args.prepare_only:
        ndk_path(args.ndk)  # Fail before downloads if the NDK is missing.
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / "build.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BuildError("Another android-memleak build is running") from exc
        print("Resolve source versions", flush=True)
        resolved = resolve_sources(config, args.offline)
        print(
            f"BCC: {resolved['bcc']['version']} ({resolved['bcc']['commit']})",
            flush=True,
        )
        sources, provenance = {}, {}
        for name, source in resolved.items():
            sources[name], provenance[name] = prepare(
                name, source, patches(name), args.offline
            )
        write_json(BUILD / "source-lock.json", provenance)
        if args.prepare_only:
            print(f"Sources ready: {BUILD / 'source-lock.json'}")
        else:
            build(args, config, sources, provenance)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BuildError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

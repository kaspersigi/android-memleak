"""Dependency-free regression tests; no network, NDK or Android device needed."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "builder", ROOT / "scripts/build-memleak.py"
)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = json.loads(b.CONFIG.read_text())
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.patcher = patch.multiple(
            b,
            CACHE=self.cache,
            BUILD=self.root / "build",
            SOURCES=self.root / "sources",
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def archive(self, members=None):
        archive = self.root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name, value in (members or {"source/file.txt": b"before\n"}).items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                bundle.addfile(info, io.BytesIO(value))
        return archive

    def make_patch(self, old="before", new="after"):
        path = self.root / "test.patch"
        path.write_text(
            f"diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-{old}\n+{new}\n"
        )
        return path

    def elf(self, *, machine=183, kinds=(1,), alignment=16384):
        data = bytearray(64 + len(kinds) * 56)
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", data, 16, 2, machine)
        struct.pack_into("<Q", data, 32, 64)
        struct.pack_into("<HH", data, 54, 56, len(kinds))
        for i, kind in enumerate(kinds):
            struct.pack_into(
                "<IIQQQQQQ",
                data,
                64 + 56 * i,
                kind,
                5,
                0,
                0x400000,
                0,
                len(data),
                len(data),
                alignment,
            )
        path = self.root / "memleak"
        path.write_bytes(data)
        return path

    def test_current_config_valid(self):
        b.validate_config(self.config)

    def test_top_level_manifest_is_default(self):
        self.assertEqual(b.parser().parse_args([]).config, ROOT / "sources.lock")
        self.assertTrue((ROOT / "CMakeLists.txt").is_file())
        self.assertTrue((ROOT / "cmake/skeleton.cmake").is_file())
        self.assertTrue((ROOT / "cmake/elfutils-config.h.in").is_file())
        self.assertTrue((ROOT / "compat/argp.cpp").is_file())
        self.assertEqual(b.OUT, ROOT / "dist")
        for component in ("bcc", "libbpf"):
            self.assertTrue(
                all(
                    p.parent == ROOT / "patches" / component
                    for p in b.patches(component)
                )
            )

    def test_cli_from_another_directory(self):
        result = subprocess.run(
            [b.sys.executable, str(ROOT / "scripts/build-memleak.py"), "--help"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("sources.lock", result.stdout)

    def test_prepared_sources_are_separate_from_build(self):
        archive = self.archive()
        with patch.object(b, "download", return_value=(archive, b.digest(archive))):
            tree, _ = b.prepare("bcc", {}, [self.make_patch()])
        self.assertEqual(tree.parent, self.root / "sources")
        self.assertFalse((self.root / "build").exists())
        self.assertEqual((tree / "file.txt").read_text(), "after\n")

    def test_invalid_versions_rejected(self):
        for value in (None, "master", "../v1.0.0", "v1.0.0;echo", "v0.37.0-rc1"):
            with self.subTest(value=value), self.assertRaises(b.BuildError):
                b.validate_config(dict(self.config, bcc=value))

    def test_invalid_api_rejected(self):
        for value in (True, 27, "35", 0):
            with self.subTest(value=value), self.assertRaises(b.BuildError):
                b.validate_config(dict(self.config, android_api=value))

    def test_dependencies_require_commit(self):
        config = copy.deepcopy(self.config)
        config["libbpf"]["commit"] = "v1.7.0"
        with self.assertRaises(b.BuildError):
            b.validate_config(config)

    def test_elfutils_requires_digest(self):
        config = copy.deepcopy(self.config)
        config["elfutils"]["sha256"] = "bad"
        with self.assertRaises(b.BuildError):
            b.validate_config(config)

    def test_build_failure_keeps_previous_artifact(self):
        ndk = self.root / "ndk"
        ndk.mkdir()
        (ndk / "source.properties").write_text("Pkg.Revision = 27.3.13750724\n")
        output = self.root / "out"
        output.mkdir()
        (output / "memleak").write_bytes(b"previous good binary")
        args = b.parser().parse_args([])
        with patch.object(b, "OUT", output), patch.object(
            b, "ndk_path", return_value=ndk
        ), patch.object(b.shutil, "which", return_value="/bin/true"), patch.object(
            b, "run", side_effect=["clang", "bpftool", b.BuildError("compiler failure")]
        ) as run:
            with self.assertRaisesRegex(b.BuildError, "compiler failure"):
                b.build(args, self.config, {}, {})
        configure = run.call_args.args[0]
        self.assertEqual(configure[configure.index("-S") + 1], ROOT)
        self.assertEqual(
            configure[configure.index("-B") + 1].parent,
            self.root / "build/android-arm64",
        )
        self.assertEqual((output / "memleak").read_bytes(), b"previous good binary")

    def test_annotated_release_resolved_and_offline_cached(self):
        answers = [
            {"tag_name": "v0.37.0", "draft": False, "prerelease": False},
            {"object": {"type": "tag", "sha": "a" * 40}},
            {"object": {"type": "commit", "sha": "b" * 40}},
        ]
        with patch.object(b, "api", side_effect=answers) as api:
            resolved = b.resolve_sources(self.config)
            self.assertEqual(api.call_count, 3)
        self.assertEqual(resolved["bcc"]["commit"], "b" * 40)
        with patch.object(b, "api", side_effect=AssertionError("network")):
            self.assertEqual(b.resolve_sources(self.config, offline=True), resolved)

    def test_prerelease_rejected(self):
        with patch.object(
            b, "api", return_value={"tag_name": "v0.38.0", "prerelease": True}
        ):
            with self.assertRaises(b.BuildError):
                b.resolve_sources(self.config)

    def test_pinned_release_mismatch_rejected(self):
        with patch.object(b, "api", return_value={"tag_name": "v0.38.0"}):
            with self.assertRaises(b.BuildError):
                b.resolve_sources(dict(self.config, bcc="v0.37.0"))

    def test_offline_missing_resolution(self):
        with self.assertRaisesRegex(b.BuildError, "No cached resolution"):
            b.resolve_sources(self.config, offline=True)

    def test_offline_missing_archive(self):
        with self.assertRaisesRegex(b.BuildError, "not cached"):
            b.download({"url": "https://example.test/source"}, offline=True)

    def test_download_and_cache_verified(self):
        source = {"url": "https://example.test/source"}
        with patch.object(b, "request", return_value=io.BytesIO(b"source bytes")):
            archive, sha = b.download(source)
        with patch.object(b, "request", side_effect=AssertionError("network")):
            self.assertEqual(b.download(source, offline=True), (archive, sha))
        archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(b.BuildError, "Corrupt cached"):
            b.download(source, offline=True)

    def test_download_expected_digest_rejected(self):
        source = {"url": "https://example.test/source", "sha256": "a" * 64}
        with patch.object(b, "request", return_value=io.BytesIO(b"bad")):
            with self.assertRaisesRegex(b.BuildError, "SHA256 mismatch"):
                b.download(source)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_archive_traversal_rejected(self):
        archive = self.archive({"../../escape": b"bad"})
        destination = self.root / "extract"
        destination.mkdir()
        with self.assertRaises(tarfile.FilterError):
            b.extract(archive, destination)
        self.assertFalse((self.root / "escape").exists())

    def test_archive_external_symlink_rejected(self):
        archive = self.root / "unsafe.tar"
        with tarfile.open(archive, "w") as bundle:
            info = tarfile.TarInfo("source/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            bundle.addfile(info)
        with self.assertRaises(tarfile.FilterError):
            b.extract(archive, self.root / "extract")

    def test_archive_internal_symlink_preserved(self):
        archive = self.archive()
        with tarfile.open(self.root / "links.tar", "w") as bundle:
            info = tarfile.TarInfo("source/vmlinux_614.h")
            info.size = 1
            bundle.addfile(info, io.BytesIO(b"x"))
            info = tarfile.TarInfo("source/vmlinux.h")
            info.type = tarfile.SYMTYPE
            info.linkname = "vmlinux_614.h"
            bundle.addfile(info)
        tree = b.extract(self.root / "links.tar", self.root / "extract")
        self.assertTrue((tree / "vmlinux.h").is_symlink())
        self.assertEqual((tree / "vmlinux.h").read_text(), "x")

    def test_patch_inside_parent_git_repo_is_not_skipped(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        tree = self.root / "build/source"
        tree.mkdir(parents=True)
        (tree / "file.txt").write_text("before\n")
        b.apply_patch(tree, self.make_patch())
        self.assertEqual((tree / "file.txt").read_text(), "after\n")

    def test_conflicting_patch_preserves_source(self):
        tree = self.root / "source"
        tree.mkdir()
        (tree / "file.txt").write_text("upstream changed\n")
        with self.assertRaises(b.BuildError):
            b.apply_patch(tree, self.make_patch())
        self.assertEqual((tree / "file.txt").read_text(), "upstream changed\n")

    def test_patch_is_not_reversed(self):
        tree = self.root / "source"
        tree.mkdir()
        (tree / "file.txt").write_text("after\n")
        with self.assertRaises(b.BuildError):
            b.apply_patch(tree, self.make_patch())
        self.assertEqual((tree / "file.txt").read_text(), "after\n")

    def test_prepared_tree_changes_rejected(self):
        archive = self.archive()
        with patch.object(b, "download", return_value=(archive, b.digest(archive))):
            tree, _ = b.prepare("bcc", {}, [self.make_patch()])
            (tree / "file.txt").write_text("edited\n")
            with self.assertRaisesRegex(b.BuildError, "sources were modified"):
                b.prepare("bcc", {}, [self.make_patch()])

    def test_patch_change_uses_separate_tree(self):
        archive = self.archive()
        with patch.object(b, "download", return_value=(archive, b.digest(archive))):
            first, _ = b.prepare("bcc", {}, [self.make_patch()])
            second, _ = b.prepare("bcc", {}, [self.make_patch(new="newer")])
        self.assertNotEqual(first, second)
        self.assertEqual((first / "file.txt").read_text(), "after\n")
        self.assertEqual((second / "file.txt").read_text(), "newer\n")

    def test_current_patch_series(self):
        self.assertEqual(len(b.patches()), 3)
        self.assertEqual(len(b.patches("libbpf")), 1)
        self.assertEqual(b.patches("zstd"), [])

    def test_empty_bcc_series_rejected(self):
        directory = self.root / "patches/bcc"
        directory.mkdir(parents=True)
        (directory / "series").write_text("# removed all patches\n\n")
        with patch.object(b, "ROOT", self.root), self.assertRaisesRegex(
            b.BuildError, "must not be empty"
        ):
            b.patches()

    def test_api_authorization_not_forwarded_on_redirect(self):
        req = b.urllib.request.Request(
            "https://api.github.com/repos/test",
            headers={"Authorization": "Bearer TEST_TOKEN"},
        )
        redirected = b.SafeRedirect().redirect_request(
            req, None, 302, "redirect", {}, "https://example.test/new"
        )
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_http_redirect_rejected(self):
        req = b.urllib.request.Request("https://example.test/file")
        with self.assertRaisesRegex(b.BuildError, "HTTPS-to-HTTP"):
            b.SafeRedirect().redirect_request(
                req, None, 302, "redirect", {}, "http://example.test/file"
            )

    def test_valid_static_elf(self):
        self.assertTrue(b.verify_elf(self.elf())["static"])

    def test_wrong_architecture_rejected(self):
        with self.assertRaisesRegex(b.BuildError, "ARM64"):
            b.verify_elf(self.elf(machine=62))

    def test_dynamic_elf_rejected(self):
        for kind in (2, 3):
            with self.subTest(kind=kind), self.assertRaisesRegex(
                b.BuildError, "not fully static"
            ):
                b.verify_elf(self.elf(kinds=(1, kind)))

    def test_4k_alignment_rejected(self):
        with self.assertRaisesRegex(b.BuildError, "16 KiB"):
            b.verify_elf(self.elf(alignment=4096))

    def test_truncated_elf_rejected(self):
        path = self.elf()
        path.write_bytes(path.read_bytes()[:70])
        with self.assertRaises(b.BuildError):
            b.verify_elf(path)

    def test_no_load_segments_rejected(self):
        with self.assertRaisesRegex(b.BuildError, "no load"):
            b.verify_elf(self.elf(kinds=(4,)))


if __name__ == "__main__":
    unittest.main()

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prepare


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        upstream = root / "upstream"
        upstream.mkdir()
        git(upstream, "init", "-q")
        self.path = "package/network/services/uhttpd"
        package = upstream / self.path
        (package / "patches").mkdir(parents=True)
        (package / "Makefile").write_text("PKG_NAME:=uhttpd\nPKG_RELEASE:=1\nPKG_VERSION:=1.0\n")
        (package / "patches/obsolete.patch").write_text("old patch\n")
        (upstream / "kernel-version").write_text("old kernel\n")
        git(upstream, "add", ".")
        git(upstream, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")
        baseline = git(upstream, "rev-parse", "HEAD")
        (package / "Makefile").write_text("PKG_NAME:=uhttpd\nPKG_RELEASE:=1\nPKG_VERSION:=2.0\n")
        (package / "patches/obsolete.patch").unlink()
        (package / "new-file").write_text("new package file\n")
        (upstream / "kernel-version").write_text("new kernel\n")
        git(upstream, "add", ".")
        git(upstream, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "update")
        update = git(upstream, "rev-parse", "HEAD")
        self.tree = root / "build"
        git(root, "clone", "-q", str(upstream), str(self.tree))
        git(self.tree, "checkout", "--detach", baseline)
        self.baseline = baseline
        self.recipe = root / "recipe"
        patch_file = self.recipe / "patches/uhttpd/900-fix.patch"
        patch_file.parent.mkdir(parents=True)
        patch_file.write_bytes(b"declared backport\n")
        self.lock = json.loads((ROOT / "sources.lock.json").read_text())
        self.lock["package_updates"] = {"uhttpd": {
            "source": "openwrt", "path": self.path, "commit": update, "release": "2",
            "packages": {"uhttpd": "2.0-r2", "uhttpd-mod-ubus": "2.0-r2"},
            "patches": [{"file": "uhttpd/900-fix.patch", "commit": "a" * 40,
                         "sha256": hashlib.sha256(patch_file.read_bytes()).hexdigest()}]}}
        context = patch.object(prepare, "HERE", self.recipe)
        context.start()
        self.addCleanup(context.stop)

    def apply(self):
        prepare.validate_lock(self.lock)
        prepare.apply_package_updates(self.tree, self.lock, "openwrt")

    def test_import_is_limited_to_package_and_removes_obsolete_patches(self):
        self.apply()
        self.assertEqual(git(self.tree, "rev-parse", "HEAD"), self.baseline)
        self.assertEqual((self.tree / "kernel-version").read_text(), "old kernel\n")
        self.assertFalse((self.tree / self.path / "patches/obsolete.patch").exists())
        self.assertTrue((self.tree / self.path / "new-file").exists())
        self.assertIn("PKG_RELEASE:=2", (self.tree / self.path / "Makefile").read_text())
        self.assertEqual(prepare.verify_package_updates(self.tree, self.lock), self.lock["package_updates"])

    def test_source_changes_after_prepare_are_rejected(self):
        self.apply()
        (self.tree / self.path / "Makefile").write_text("PKG_VERSION:=unreviewed\n")
        with self.assertRaisesRegex(ValueError, "content differs"):
            prepare.verify_package_updates(self.tree, self.lock)

    def test_unregistered_patch_is_rejected(self):
        self.apply()
        (self.tree / self.path / "patches/extra.patch").write_text("unreviewed\n")
        with self.assertRaisesRegex(ValueError, "file set differs"):
            prepare.verify_package_updates(self.tree, self.lock)

    def test_changed_backport_checksum_is_rejected(self):
        (self.recipe / "patches/uhttpd/900-fix.patch").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.apply()

    def test_old_or_missing_module_versions_are_rejected(self):
        manifest = "uhttpd - 2.0-r2\nuhttpd-mod-ubus - 2.0-r2\n"
        prepare.validate_updated_packages(manifest, self.lock)
        for invalid in (manifest.replace("uhttpd - 2.0-r2", "uhttpd - 1.0-r1"), "uhttpd - 2.0-r2\n"):
            with self.subTest(manifest=invalid), self.assertRaisesRegex(ValueError, "Security update missing"):
                prepare.validate_updated_packages(invalid, self.lock)

    def test_rolling_refs_and_nonpackage_paths_are_rejected(self):
        for key, value in (("commit", "main"), ("path", "../package/uhttpd"),
                           ("path", "target/linux"), ("source", "unknown")):
            lock = copy.deepcopy(self.lock)
            lock["package_updates"]["uhttpd"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                prepare.validate_lock(lock)

    def test_source_override_requires_stable_version_and_checksum(self):
        lock = copy.deepcopy(self.lock)
        update = lock["package_updates"]["uhttpd"]
        update.update(version="1.102.3", sha256="f" * 64)
        prepare.validate_lock(lock)
        original = b"PKG_VERSION:=1.98.3\nPKG_RELEASE:=1\nPKG_HASH:=" + b"a" * 64 + b"\n"
        result = prepare.updated_makefile(original, update)
        self.assertIn(b"PKG_VERSION:=1.102.3\n", result)
        self.assertIn(b"PKG_HASH:=" + b"f" * 64, result)
        for value in ("1.103.0-rc1", "nightly", "1.102.3\nother"):
            update["version"] = value
            with self.assertRaisesRegex(ValueError, "stable version"):
                prepare.validate_lock(lock)

    def test_host_toolchain_version_is_checked_without_installing_a_compiler_on_the_router(self):
        lock = {"package_updates": {"golang": {"host_version": "1.26.6", "packages": {}}}}
        with patch.object(prepare.subprocess, "check_output", return_value="go version go1.26.6 linux/amd64\n"):
            self.assertEqual(prepare.verify_build_tools(self.tree, lock), {"golang": "1.26.6"})
        with patch.object(prepare.subprocess, "check_output", return_value="go version go1.26.3 linux/amd64\n"):
            with self.assertRaisesRegex(ValueError, "toolchain version"):
                prepare.verify_build_tools(self.tree, lock)


if __name__ == "__main__":
    unittest.main()

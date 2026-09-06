import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ImplementationExists(unittest.TestCase):
    def test_validation_modules_exist(self):
        for name in ("verify", "inject_defaults", "prepare"):
            with self.subTest(name=name):
                self.assertIsNotNone(importlib.util.find_spec(name))


@unittest.skipUnless((ROOT / "verify.py").exists(), "implementation pending")
class ValidationTests(unittest.TestCase):
    def test_upgrade_guard_accepts_only_the_observed_layout(self):
        script = ROOT / "files/sl3000-upgrade.sh"
        harness = r'''
find_mmc_part() {
    case "$1" in
        kernel) echo /dev/mmcblk0p1;;
        rootfs) echo /dev/mmcblk0p2;;
        storage) echo /dev/mmcblk0p3;;
    esac
}
cat() {
    case "$1" in
        */mmcblk0p1/start) echo "${KERNEL_START:-8192}";;
        */mmcblk0p1/size) echo 65536;;
        */mmcblk0p2/start) echo 73728;;
        */mmcblk0p2/size) echo 4096000;;
        */mmcblk0p3/start) echo "${STORAGE_START:-4169728}";;
        *) return 1;;
    esac
}
. "$1"
sl3000_check_image "$2"
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.bin"
            with tarfile.open(path, "w") as archive:
                for name in ("kernel", "root"):
                    info = tarfile.TarInfo("sysupgrade-sl_3000-emmc/" + name)
                    info.size = 4
                    archive.addfile(info, io.BytesIO(b"test"))
            command = ["sh", "-c", harness, "test", str(script), str(path)]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for key in ("KERNEL_START", "STORAGE_START"):
                result = subprocess.run(command, capture_output=True, env={**os.environ, key: "2048"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"Unsupported SL-3000 partition layout", result.stdout)

    def test_source_context_drift_is_rejected(self):
        from prepare import replace_once
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.sh"
            for text in ("missing", "anchor\nanchor"):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    replace_once(path, "anchor", "replacement")
                self.assertEqual(path.read_text(), text)

    def test_proxy_packages_cannot_resolve_to_the_official_feed(self):
        from prepare import PROXY_PACKAGES, validate_package_sources
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            for package in PROXY_PACKAGES:
                source = tree / "feeds/passwall_packages" / package
                source.mkdir(parents=True)
                link = tree / "package/feeds/passwall_packages" / package
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(source)
            validate_package_sources(tree)
            wrong = tree / "package/feeds/packages/sing-box"
            wrong.parent.mkdir(parents=True)
            wrong.symlink_to(tree / "feeds/packages/net/sing-box")
            with self.assertRaises(ValueError):
                validate_package_sources(tree)

    def test_source_lock_rejects_rolling_ref(self):
        from prepare import validate_lock
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        validate_lock(lock)
        lock["openwrt"]["commit"] = "main"
        with self.assertRaises(ValueError):
            validate_lock(lock)

    def test_required_packages_must_be_built_in(self):
        from verify import validate_config
        config = (ROOT / ".config").read_text()
        validate_config(config)
        for package in ("tailscale", "kmod-nft-socket", "luci-app-openclash"):
            for setting in ("m", "n"):
                with self.subTest(package=package, setting=setting):
                    with self.assertRaises(ValueError):
                        validate_config(config.replace(f"CONFIG_PACKAGE_{package}=y", f"CONFIG_PACKAGE_{package}={setting}"))

    def test_rejects_multiple_targets(self):
        from verify import validate_config
        with self.assertRaises(ValueError):
            validate_config((ROOT / ".config").read_text() + "\nCONFIG_TARGET_mediatek_filogic_DEVICE_other=y\n")

    def test_manifest_requires_all_packages(self):
        from verify import REQUIRED_PACKAGES, validate_manifest
        manifest = "\n".join(f"{p} - 1.0" for p in REQUIRED_PACKAGES)
        validate_manifest(manifest)
        with self.assertRaises(ValueError):
            validate_manifest(manifest.replace("kmod-nft-tproxy - 1.0", ""))

    def test_metadata_requires_exact_board(self):
        from verify import validate_metadata
        validate_metadata({"supported_devices": ["sl,3000-emmc"]})
        for boards in ([], ["sl,3000"], ["sl,3000-emmc", "other"]):
            with self.subTest(boards=boards), self.assertRaises(ValueError):
                validate_metadata({"supported_devices": boards})

    def test_tar_rejects_unsafe_payloads(self):
        from verify import validate_tar
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sysupgrade.bin"
            def build(extra=None):
                with tarfile.open(path, "w") as archive:
                    files = {"CONTROL": b"BOARD=sl_3000-emmc\n", "kernel": b"\xd0\x0d\xfe\xedtest", "root": b"hsqstest"}
                    if extra:
                        files.update(extra)
                    for name, data in files.items():
                        info = tarfile.TarInfo("sysupgrade-sl_3000-emmc/" + name)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
            build()
            validate_tar(path)
            for extra in ({"fip": b"bootloader"}, {"../../escape": b"bad"}):
                build(extra)
                with self.assertRaises(ValueError):
                    validate_tar(path)

    def test_tar_checks_partition_capacity(self):
        from verify import validate_payload_sizes
        validate_payload_sizes(10 * 1024**2, 300 * 1024**2)
        for sizes in ((33 * 1024**2, 10), (10, 2000 * 1024**2), (0, 10)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                validate_payload_sizes(*sizes)


@unittest.skipUnless((ROOT / "inject_defaults.py").exists(), "implementation pending")
class DefaultsTests(unittest.TestCase):
    def test_preserved_upgrade_skips_all_setup_defaults(self):
        script = (ROOT / "firstboot.sh").read_text()
        # Exercise the guard before any library, UCI or service side effects.
        prefix = script.split('. /lib/functions.sh', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            board = root / "board_name"
            board.write_text("sl,3000-emmc\n")
            prefix = prefix.replace("/tmp/sysinfo/board_name", str(board))
            prefix = prefix.replace("/sysupgrade.tgz", str(root / "sysupgrade.tgz"))
            prefix = prefix.replace("/tmp/sysupgrade.tar", str(root / "sysupgrade.tar"))
            harness = 'logger() { :; }\n' + prefix + '\nprintf "defaults-would-run\\n"\n'
            for backup_name in ("sysupgrade.tgz", "sysupgrade.tar"):
                with self.subTest(backup=backup_name):
                    backup = root / backup_name
                    backup.write_bytes(b"restored configuration")
                    result = subprocess.run(["sh", "-c", harness], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(backup.read_bytes(), b"restored configuration")
                    backup.unlink()
            result = subprocess.run(["sh", "-c", harness], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "defaults-would-run\n")

    def test_passwall_service_is_enabled_without_enabling_proxy(self):
        script = (ROOT / "firstboot.sh").read_text()
        defaults = script[script.index("# These packages"):script.index("# Mainline mt76")]
        services = ("passwall", "passwall_server", "openclash", "tailscale", "miniupnpd")
        with tempfile.TemporaryDirectory() as directory:
            init = Path(directory) / "init.d"
            init.mkdir()
            for service in services:
                stub = init / service
                stub.write_text('#!/bin/sh\nprintf "service:%s:%s\\n" "${0##*/}" "$1"\n')
                stub.chmod(0o700)
            # Execute the real service-default stanza with harmless UCI/init stubs.
            harness = 'uci() { printf "uci:%s\\n" "$*"; }\n'
            harness += defaults.replace("/etc/init.d/", f"{init}/")
            result = subprocess.run(
                ["sh", "-c", harness], capture_output=True, text=True, check=True,
            )
        actions = result.stdout.splitlines()
        self.assertEqual(
            [action for action in actions if action.startswith("service:passwall:")],
            ["service:passwall:enable"],
        )
        for option in ("enabled", "acl_enable"):
            self.assertIn(f"uci:-q set passwall.@global[0].{option}=0", actions)
        for service in services[1:]:
            self.assertEqual(
                [action for action in actions if action.startswith(f"service:{service}:")],
                [f"service:{service}:disable"],
            )

    def test_injects_only_the_rootfs_script(self):
        from inject_defaults import inject
        password = "temporary-test-password"
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            inject(tree, password)
            script = tree / "files/etc/uci-defaults/99-sl3000-setup"
            self.assertEqual(script.stat().st_mode & 0o777, 0o700)
            self.assertIn(password, script.read_text())
            self.assertNotIn("# WIFI_PASSWORD_INJECTED_HERE", script.read_text())
            subprocess.run(["sh", "-n", str(script)], check=True)
            self.assertEqual([p for p in tree.rglob("*") if p.is_file()], [script])

    def test_invalid_passwords_are_rejected_without_echoing(self):
        from inject_defaults import validate_password
        for password in ("", "1234567", "a" * 64, "12345678\n", "\u4e2d" * 8, "\x0012345678"):
            with self.subTest(length=len(password)), self.assertRaises(ValueError):
                validate_password(password)

    def test_password_is_quoted_not_executed(self):
        from inject_defaults import shell_assignment
        password = "a'b\"c$HOME`false`\\123"
        result = subprocess.run(["sh", "-c", shell_assignment(password) + '\nprintf "%s" "$wifi_password"'], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, password)


if __name__ == "__main__":
    unittest.main()

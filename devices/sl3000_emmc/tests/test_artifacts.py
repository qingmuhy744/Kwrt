import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import verify
import rf_test
import nor_probe
from test_rf_test import fixture, private_env

IMAGE_PREFIX = f"openwrt-25.12.5-mediatek-filogic-{verify.PROFILE}"
INITRAMFS_NAME = f"{IMAGE_PREFIX}-initramfs.itb"
PASSWORD = "temporary-test-password"


class ArtifactCollectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tree = Path(directory.name) / "openwrt"
        self.output = Path(directory.name) / "release"
        self.target = self.tree / "bin/targets/mediatek/filogic"
        self.target.mkdir(parents=True)
        (self.tree / ".config").write_text((ROOT / ".config").read_text())
        self.sysupgrade = self.target / f"{IMAGE_PREFIX}-squashfs-sysupgrade.bin"
        with tarfile.open(self.sysupgrade, "w") as archive:
            for name, data in {
                "CONTROL": f"BOARD={verify.PROFILE}\n".encode(),
                "kernel": b"\xd0\x0d\xfe\xedtest",
                "root": b"hsqstest",
            }.items():
                info = tarfile.TarInfo(f"sysupgrade-{verify.PROFILE}/{name}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        self.initramfs = self.target / INITRAMFS_NAME
        self.initramfs.write_bytes(b"\xd0\x0d\xfe\xedtest")
        self.manifest = self.target / f"{IMAGE_PREFIX}.manifest"
        self.manifest.write_text("\n".join(f"{name} - 1.0" for name in verify.REQUIRED_PACKAGES))
        kernel_config = self.tree / "build_dir/target-aarch64/linux-mediatek_filogic/linux-6.12.1/.config"
        kernel_config.parent.mkdir(parents=True)
        kernel_config.write_text("CONFIG_MMC=y\nCONFIG_MMC_BLOCK=y\nCONFIG_MMC_MTK=y\n")
        self.metadata = {"supported_devices": [verify.BOARD]}

    def read_metadata(self, command, *, check):
        self.assertTrue(check)
        self.assertEqual(command[:2], [str(self.tree / "staging_dir/host/bin/fwtool"), "-i"])
        self.assertEqual(command[3:], [str(self.sysupgrade)])
        Path(command[2]).write_text(json.dumps(self.metadata))
        return subprocess.CompletedProcess(command, 0)

    def collect(self, rootfs_error=None):
        # Only external firmware inspection is mocked; collection and safety checks run normally.
        with patch.dict(os.environ, {"DEFAULT_WIFI_PASSWORD": PASSWORD}), \
                patch.object(verify.subprocess, "run", side_effect=self.read_metadata) as fwtool, \
                patch.object(verify, "verify_rootfs", side_effect=rootfs_error) as rootfs, \
                contextlib.redirect_stdout(io.StringIO()):
            verify.artifacts(self.tree, self.output)
            fwtool.assert_called_once()
            rootfs.assert_called_once()
            self.assertEqual(rootfs.call_args.args[0], self.sysupgrade)
            self.assertEqual(rootfs.call_args.args[2], PASSWORD)

    def test_initramfs_name_matches_device_recipe(self):
        makefile = f"""DEVICE_IMG_PREFIX := {IMAGE_PREFIX}
# Naming convention from the pinned OpenWrt include/image.mk.
KERNEL_INITRAMFS_PREFIX = $(DEVICE_IMG_PREFIX)-initramfs
KERNEL_INITRAMFS_IMAGE = $(KERNEL_INITRAMFS_PREFIX)$(KERNEL_INITRAMFS_SUFFIX)
include image.mk
$(eval $(Device/sl_3000-emmc))
.PHONY: inspect
inspect:
\t@printf '%s\\n' '$(KERNEL_INITRAMFS_IMAGE)'
"""
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", "-", "inspect"],
            input=makefile, cwd=ROOT, text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), INITRAMFS_NAME)

    def test_collects_expected_images_and_checksums(self):
        for name in ("fip.bin", "bl2.bin", "gpt.bin", "other-initramfs.itb"):
            (self.target / name).write_bytes(b"not an approved artifact")
        self.collect()
        expected = {
            self.sysupgrade.name, INITRAMFS_NAME, self.manifest.name,
            "openwrt.config", "kernel.config", "sources.lock.json", "README.md",
            "build-info.json", "sha256sums",
        }
        self.assertEqual({path.name for path in self.output.iterdir()}, expected)
        checksums = {}
        for line in (self.output / "sha256sums").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, verify.sha256(self.output / name))
            checksums[name] = digest
        self.assertEqual(set(checksums), expected - {"sha256sums"})
        self.assertFalse(json.loads((self.output / "build-info.json").read_text())["hardware_validated"])

    def test_missing_or_misnamed_initramfs_is_rejected(self):
        self.initramfs.unlink()
        for name in (None, f"{IMAGE_PREFIX}-initramfs-kernel.itb", "openwrt-other-initramfs.itb"):
            with self.subTest(name=name):
                if name:
                    (self.target / name).write_bytes(b"\xd0\x0d\xfe\xedtest")
                with self.assertRaisesRegex(ValueError, "Expected one initramfs, found 0"):
                    self.collect()
                self.assertFalse(self.output.exists())
                if name:
                    (self.target / name).unlink()

    def test_duplicate_initramfs_is_rejected(self):
        (self.target / f"old-{verify.PROFILE}-initramfs.itb").write_bytes(self.initramfs.read_bytes())
        with self.assertRaisesRegex(ValueError, "Expected one initramfs, found 2"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_non_fit_initramfs_is_rejected(self):
        self.initramfs.write_bytes(b"not a FIT image")
        with self.assertRaisesRegex(ValueError, "Initramfs image is not FIT"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_wrong_board_metadata_is_rejected(self):
        self.metadata = {"supported_devices": ["sl,3000"]}
        with self.assertRaisesRegex(ValueError, "Image metadata does not exclusively support"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_rootfs_validation_failure_prevents_export(self):
        with self.assertRaisesRegex(ValueError, "Invalid rootfs fixture"):
            self.collect(rootfs_error=ValueError("Invalid rootfs fixture"))
        self.assertFalse(self.output.exists())

    def test_nor_profile_checks_both_fit_payloads_before_export(self):
        from test_nor_probe import kernel_config, probe_env
        config = next(self.tree.glob("build_dir/target-*/linux-mediatek_filogic/linux-6.12*/.config"))
        config.write_text(config.read_text() + kernel_config())
        with patch.dict(os.environ, probe_env()), \
                patch.object(nor_probe, "validate_fit") as inspect:
            self.collect()
            self.assertEqual(inspect.call_count, 2)
            self.assertEqual(inspect.call_args_list[0].args[0].name, "kernel.fit")
            self.assertEqual(inspect.call_args_list[1].args[0], self.initramfs)
        info = json.loads((self.output / "build-info.json").read_text())
        self.assertEqual(info["nor_profile"], "read-only-probe")
        self.assertEqual(info["wifi_profile"], "private-runtime-eeprom-ab-test")

    def test_invalid_nor_fit_prevents_export(self):
        from test_nor_probe import probe_env
        with patch.dict(os.environ, probe_env()), \
                patch.object(nor_probe, "validate_fit", side_effect=ValueError("unsafe NOR fixture")):
            with self.assertRaisesRegex(ValueError, "unsafe NOR fixture"):
                self.collect()
        self.assertFalse(self.output.exists())


class RootfsRFTests(unittest.TestCase):
    def test_private_rootfs_requires_exact_eeprom_and_marker(self):
        from inject_defaults import shell_assignment
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            image = scratch / "image.bin"
            with tarfile.open(image, "w") as archive:
                info = tarfile.TarInfo(f"sysupgrade-{verify.PROFILE}/root")
                info.size = 4
                archive.addfile(info, io.BytesIO(b"hsqs"))
            core = bytearray(20)
            core[:6] = b"\x7fELF\x02\x01"
            core[18:20] = b"\xb7\x00"
            files = {
                "etc/uci-defaults/99-sl3000-setup": (ROOT / "firstboot.sh").read_text().replace(
                    "# WIFI_PASSWORD_INJECTED_HERE", shell_assignment(PASSWORD)).encode(),
                "etc/openclash/core/clash_meta": bytes(core),
                rf_test.FIRMWARE_PATH: fixture(),
                rf_test.MARKER_PATH: rf_test.marker(fixture()),
                "etc/shadow": b"root::0:0:99999:7:::\n",
            }
            with patch.dict(os.environ, private_env()), \
                    patch.object(verify, "root_file", side_effect=lambda _, name: files[name]), \
                    patch.object(verify.subprocess, "check_output", return_value=""):
                verify.verify_rootfs(image, scratch, PASSWORD)
                files[rf_test.FIRMWARE_PATH] = b"\0" * rf_test.SIZE
                with self.assertRaisesRegex(ValueError, "not embedded exactly"):
                    verify.verify_rootfs(image, scratch, PASSWORD)
                files[rf_test.FIRMWARE_PATH] = fixture()
                files[rf_test.MARKER_PATH] = b"wrong marker"
                with self.assertRaisesRegex(ValueError, "marker missing or changed"):
                    verify.verify_rootfs(image, scratch, PASSWORD)
                files[rf_test.MARKER_PATH] = rf_test.marker(fixture())
                with patch.dict(os.environ, {"SL3000_NOR_PROBE": "true"}):
                    files[nor_probe.MARKER_PATH] = b"wrong NOR marker"
                    with self.assertRaisesRegex(ValueError, "NOR probe marker"):
                        verify.verify_rootfs(image, scratch, PASSWORD)
                    files[nor_probe.MARKER_PATH] = nor_probe.marker()
                    verify.verify_rootfs(image, scratch, PASSWORD)

    def test_private_secret_cannot_be_omitted_to_use_generic_eeprom(self):
        with patch.dict(os.environ, {"SL3000_RF_TEST": "true", rf_test.CALIBRATION_ENV: ""}):
            with self.assertRaisesRegex(ValueError, "private RF calibration"):
                rf_test.calibration()

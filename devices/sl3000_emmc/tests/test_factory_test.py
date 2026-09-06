import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import factory_test
import nor_probe
import rf_test
from test_rf_test import fixture
from test_nor_probe import kernel_config as nor_kernel_config


def public_env():
    return {"SL3000_FACTORY_TEST": "true", "SL3000_RF_TEST": "false", "SL3000_NOR_PROBE": "false"}


def kernel_config():
    return nor_kernel_config() + "CONFIG_NVMEM=y\nCONFIG_NVMEM_LAYOUTS=y\n"


class FactoryProfileTests(unittest.TestCase):
    def test_public_profile_does_not_need_private_secrets_and_forbids_release(self):
        with patch.dict(os.environ, public_env(), clear=True):
            rf_test.preflight(False)
            with self.assertRaisesRegex(ValueError, "artifact-only"):
                rf_test.preflight(True)
        with patch.dict(os.environ, {"SL3000_FACTORY_TEST": "invalid"}, clear=True):
            with self.assertRaises(ValueError):
                factory_test.enabled()

    def test_public_private_conflicts_and_secrets_are_rejected(self):
        for name in ("SL3000_RF_TEST", "SL3000_NOR_PROBE", rf_test.CALIBRATION_ENV, rf_test.PASSPHRASE_ENV):
            with self.subTest(name=name), patch.dict(os.environ, {**public_env(), name: "true"}, clear=True):
                with self.assertRaises(ValueError):
                    rf_test.preflight(False)
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(ValueError):
                        rf_test.inject(Path(directory))
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_opt_in_prepare_only_adds_public_dts_policy_and_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            dts = tree / "target/linux/mediatek/dts/mt7981b-sl-3000-emmc.dts"
            dts.parent.mkdir(parents=True)
            base = (ROOT / "files" / dts.name).read_text()
            dts.write_text(base)
            config = tree / "target/linux/mediatek/filogic/config-6.12"
            config.parent.mkdir(parents=True)
            config.write_text("CONFIG_EXISTING=y\nCONFIG_MTD_SPI_NOR_SWP_DISABLE=y\n")
            with patch.dict(os.environ, {}, clear=True):
                factory_test.prepare(tree)
                self.assertEqual(dts.read_text(), base)
                self.assertFalse((tree / "files").exists())
            with patch.dict(os.environ, public_env(), clear=True):
                factory_test.prepare(tree)
                self.assertEqual(dts.read_text(), base + f'\n#include "{factory_test.FRAGMENT}"\n')
                marker = tree / "files" / factory_test.MARKER_PATH
                self.assertEqual(marker.read_bytes(), factory_test.marker())
                self.assertEqual([p for p in (tree / "files").rglob("*") if p.is_file()], [marker])
                self.assertIn("CONFIG_EXISTING=y", config.read_text())
                self.assertNotIn("CONFIG_MTD_SPI_NOR_SWP_DISABLE=y", config.read_text())
                self.assertIn("CONFIG_NVMEM=y", config.read_text())
                with self.assertRaisesRegex(ValueError, "already prepared"):
                    factory_test.prepare(tree)

    def test_existing_eeprom_overlay_is_rejected_before_edits(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, public_env(), clear=True):
            tree = Path(directory)
            path = tree / "files" / rf_test.FIRMWARE_PATH
            path.parent.mkdir(parents=True)
            path.write_bytes(fixture())
            with self.assertRaisesRegex(ValueError, "private overlay"):
                factory_test.prepare(tree)
            self.assertEqual(path.read_bytes(), fixture())

    def test_kernel_requires_nvmem_and_read_only_policy(self):
        text = kernel_config()
        factory_test.validate_kernel(text)
        for symbol in ("CONFIG_NVMEM", "CONFIG_NVMEM_LAYOUTS", "CONFIG_MTD_SPI_NOR_SWP_KEEP"):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                factory_test.validate_kernel(text.replace(f"{symbol}=y", f"{symbol}=m"))
        with self.assertRaises(ValueError):
            factory_test.validate_kernel(text + "CONFIG_MTD_PARTITIONED_MASTER=y\n")


class FactoryFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Synthetic test bytes only, never calibration captured from a router.
        self.eeprom = fixture()
        (self.root / "sources.lock.json").write_text(json.dumps({
            "mt76_eeprom": {"sha256": hashlib.sha256(self.eeprom).hexdigest()},
        }))
        self.files = {rf_test.FIRMWARE_PATH: self.eeprom, factory_test.MARKER_PATH: factory_test.marker()}

    def validate(self):
        with patch.object(factory_test, "HERE", self.root):
            factory_test.validate_files(self.files.__getitem__, self.files)

    def test_requires_exact_public_fallback_and_marker(self):
        self.validate()
        self.files[rf_test.FIRMWARE_PATH] = b"\xff" * 4096
        with self.assertRaisesRegex(ValueError, "pinned public generic"):
            self.validate()
        self.files[rf_test.FIRMWARE_PATH] = self.eeprom
        self.files[factory_test.MARKER_PATH] = b"wrong marker"
        with self.assertRaisesRegex(ValueError, "marker missing or changed"):
            self.validate()

    def test_private_markers_are_rejected_even_with_public_fallback(self):
        for name in (rf_test.MARKER_PATH, nor_probe.MARKER_PATH):
            self.files[name] = b"private marker"
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Private calibration"):
                self.validate()
            del self.files[name]

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("dtc", "fdtget", "dumpimage", "bsdtar")),
                         "FIT and CPIO inspection tools are required")
    def test_real_initramfs_payload_is_checked_for_public_fallback(self):
        from test_network import network_files
        contents = self.root / "files"
        for name, data in {**self.files, **network_files()}.items():
            path = contents / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        archive = self.root / "initrd.cpio"
        fit = self.root / "initramfs.itb"
        source = '''/dts-v1/;
/ {
    description = "Synthetic Factory test FIT";
    timestamp = <0>;
    images {
        initrd-1 { data = /incbin/ ("initrd.cpio"); type = "ramdisk"; };
    };
    configurations {
        default = "config-1";
        config-1 { ramdisk = "initrd-1"; };
    };
};
'''
        def build():
            subprocess.run(["bsdtar", "--format=newc", "-cf", str(archive), "-C", str(contents), "."], check=True)
            subprocess.run(["dtc", "-q", "-i", str(self.root), "-O", "dtb", "-o", str(fit), "-"],
                           input=source, text=True, check=True, capture_output=True)
        build()
        with patch.object(factory_test, "HERE", self.root):
            factory_test.validate_initramfs(fit, self.root)
            (contents / rf_test.FIRMWARE_PATH).write_bytes(b"\xff" * 4096)
            build()
            with self.assertRaisesRegex(ValueError, "pinned public generic"):
                factory_test.validate_initramfs(fit, self.root)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("dtc", "fdtget", "fdtput")),
                     "Device-tree compiler tools are required")
class FactoryDeviceTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dtb = self.root / "factory.dtb"
        source = '''/dts-v1/;
/ {
    compatible = "sl,3000-emmc", "mediatek,mt7981";
    #address-cells = <2>;
    #size-cells = <2>;
    chosen { bootargs = "root=PARTLABEL=rootfs rootwait"; };
    soc {
        #address-cells = <2>;
        #size-cells = <2>;
        ranges;
        spi2: spi@11009000 {
            reg = <0 0x11009000 0 0x1000>;
            #address-cells = <1>;
            #size-cells = <0>;
            status = "disabled";
        };
        pio: pinctrl@11d00000 { reg = <0 0x11d00000 0 0x1000>; };
        wifi: wifi@18000000 { reg = <0 0x18000000 0 0x1000000>; status = "okay"; };
    };
};
'''
        source += (ROOT / "files" / factory_test.FRAGMENT).read_text()
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(self.dtb), "-"],
                       input=source, text=True, check=True, capture_output=True)

    def test_factory_is_read_only_and_bound_to_wifi_without_device_bytes(self):
        factory_test.validate_dtb(self.dtb)

    def test_wrong_factory_offset_eeprom_length_or_binding_is_rejected(self):
        original = self.dtb.read_bytes()
        for node, prop, values in (
            (factory_test.PARTITION, "reg", ["0", "200000"]),
            (factory_test.EEPROM, "reg", ["0", "200000"]),
            (nor_probe.WIFI, "nvmem-cells", ["ffff"]),
            (nor_probe.SPI, "pinctrl-0", ["ffff"]),
        ):
            with self.subTest(node=node, prop=prop):
                self.dtb.write_bytes(original)
                subprocess.run(["fdtput", "-t", "x", str(self.dtb), node, prop, *values], check=True)
                with self.assertRaises(ValueError):
                    factory_test.validate_dtb(self.dtb)

    def test_writable_factory_extra_partition_and_eeprom_override_are_rejected(self):
        original = self.dtb.read_bytes()
        commands = [
            ["-d", str(self.dtb), factory_test.PARTITION, "read-only"],
            ["-c", str(self.dtb), nor_probe.PARTITIONS + "/partition@0"],
            ["-t", "x", str(self.dtb), nor_probe.WIFI, "mediatek,eeprom-data", "7981"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.dtb.write_bytes(original)
                subprocess.run(["fdtput", *command], check=True)
                with self.assertRaises(ValueError):
                    factory_test.validate_dtb(self.dtb)

    def test_all_embedded_fit_configurations_are_validated(self):
        fit = self.root / "kernel.fit"
        source = '''/dts-v1/;
/ {
    images {
        fdt-1 { data = /incbin/ ("factory.dtb"); type = "flat_dt"; compression = "none"; };
    };
    configurations {
        default = "config-1";
        config-1 { fdt = "fdt-1"; };
        config-2 { fdt = "fdt-1"; };
    };
};
'''
        subprocess.run(["dtc", "-q", "-i", str(self.root), "-O", "dtb", "-o", str(fit), "-"],
                       input=source, text=True, check=True, capture_output=True)
        factory_test.validate_fit(fit, self.root)
        subprocess.run(["fdtput", "-t", "s", str(fit), "/configurations/config-2", "fdt", "missing"], check=True)
        with self.assertRaises(ValueError):
            factory_test.validate_fit(fit, self.root)


if __name__ == "__main__":
    unittest.main()

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
import nor_probe
import rf_test
from test_rf_test import fixture, private_env


def probe_env():
    return {**private_env(), "SL3000_NOR_PROBE": "true"}


def kernel_config():
    return "\n".join([
        "CONFIG_MTD=y", "CONFIG_MTD_SPI_NOR=y", "CONFIG_SPI=y", "CONFIG_SPI_MT65XX=y",
        *(f"{key}=y" if value == "y" else f"# {key} is not set"
          for key, value in nor_probe.KERNEL_POLICY.items()),
    ]) + "\n"


class NORProfileTests(unittest.TestCase):
    def test_probe_requires_private_calibration_and_no_release(self):
        with patch.dict(os.environ, {"SL3000_NOR_PROBE": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "requires the private"):
                rf_test.preflight(False)
        with patch.dict(os.environ, probe_env(), clear=True):
            rf_test.preflight(False)
            with self.assertRaisesRegex(ValueError, "must not be published"):
                rf_test.preflight(True)
        with patch.dict(os.environ, {"SL3000_NOR_PROBE": "invalid"}, clear=True):
            with self.assertRaises(ValueError):
                nor_probe.enabled()

    def test_probe_preparation_is_opt_in_and_keeps_eeprom_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            dts = tree / "target/linux/mediatek/dts/mt7981b-sl-3000-emmc.dts"
            dts.parent.mkdir(parents=True)
            base = (ROOT / "files" / dts.name).read_text()
            dts.write_text(base)
            config = tree / "target/linux/mediatek/filogic/config-6.12"
            config.parent.mkdir(parents=True)
            config.write_text("CONFIG_EXISTING=y\nCONFIG_MTD_SPI_NOR_SWP_DISABLE_ON_VOLATILE=y\n")
            with patch.dict(os.environ, {}, clear=True):
                nor_probe.prepare(tree)
                self.assertEqual(dts.read_text(), base)
                self.assertFalse((tree / "files").exists())
            with patch.dict(os.environ, probe_env(), clear=True):
                rf_test.inject(tree)
                nor_probe.prepare(tree)
                self.assertEqual((tree / "files" / rf_test.FIRMWARE_PATH).read_bytes(), fixture())
                self.assertEqual((tree / "files" / nor_probe.MARKER_PATH).read_bytes(), nor_probe.marker())
                self.assertEqual(dts.read_text(), base + f'\n#include "{nor_probe.FRAGMENT}"\n')
                self.assertIn("CONFIG_EXISTING=y", config.read_text())
                self.assertNotIn("CONFIG_MTD_SPI_NOR_SWP_DISABLE_ON_VOLATILE=y", config.read_text())
                with self.assertRaisesRegex(ValueError, "already prepared"):
                    nor_probe.prepare(tree)

    def test_kernel_rejects_writable_master_unprotection_and_missing_driver(self):
        text = kernel_config()
        nor_probe.validate_kernel(text)
        for symbol in ("CONFIG_MTD_PARTITIONED_MASTER", "CONFIG_MTD_SPI_NOR_SWP_DISABLE",
                       "CONFIG_MTD_SPI_NOR_SWP_DISABLE_ON_VOLATILE"):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                nor_probe.validate_kernel(text.replace(f"# {symbol} is not set", f"{symbol}=y"))
        for symbol in ("CONFIG_MTD_SPI_NOR", "CONFIG_SPI_MT65XX", "CONFIG_MTD_SPI_NOR_SWP_KEEP"):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                nor_probe.validate_kernel(text.replace(f"{symbol}=y", f"{symbol}=m"))


@unittest.skipUnless(all(shutil.which(tool) for tool in ("dtc", "fdtget", "fdtput")),
                     "Device-tree compiler tools are required")
class NORDeviceTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dtb = self.root / "probe.dtb"
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
        wifi@18000000 { reg = <0 0x18000000 0 0x1000000>; status = "okay"; };
    };
};
'''
        source += (ROOT / "files" / nor_probe.FRAGMENT).read_text()
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(self.dtb), "-"],
                       input=source, text=True, check=True, capture_output=True)

    def test_compiled_fragment_has_only_read_only_nor_and_no_wifi_binding(self):
        nor_probe.validate_dtb(self.dtb)

    def test_writable_partition_is_rejected(self):
        subprocess.run(["fdtput", "-d", str(self.dtb), nor_probe.PARTITION, "read-only"], check=True)
        with self.assertRaisesRegex(ValueError, "writable"):
            nor_probe.validate_dtb(self.dtb)

    def test_extra_partition_is_rejected(self):
        subprocess.run(["fdtput", "-c", str(self.dtb), nor_probe.PARTITIONS + "/partition@180000"], check=True)
        with self.assertRaisesRegex(ValueError, "only one read-only"):
            nor_probe.validate_dtb(self.dtb)

    def test_factory_wifi_binding_is_rejected(self):
        subprocess.run(["fdtput", "-t", "x", str(self.dtb), nor_probe.WIFI, "nvmem-cells", "1"], check=True)
        with self.assertRaisesRegex(ValueError, "must not switch"):
            nor_probe.validate_dtb(self.dtb)

    def test_wrong_spi_pins_are_rejected(self):
        subprocess.run(["fdtput", "-t", "x", str(self.dtb), nor_probe.SPI, "pinctrl-0", "ffff"], check=True)
        with self.assertRaisesRegex(ValueError, "pins are not connected"):
            nor_probe.validate_dtb(self.dtb)

    def test_embedded_fit_device_tree_is_checked(self):
        source = '''/dts-v1/;
/ {
    images {
        fdt-1 { data = /incbin/ ("probe.dtb"); type = "flat_dt"; compression = "none"; };
    };
    configurations {
        default = "config-1";
        config-1 { fdt = "fdt-1"; };
    };
};
'''
        fit = self.root / "kernel.fit"
        subprocess.run(["dtc", "-q", "-i", str(self.root), "-O", "dtb", "-o", str(fit), "-"],
                       input=source, text=True, check=True, capture_output=True)
        nor_probe.validate_fit(fit, self.root)
        subprocess.run(["fdtput", "-t", "s", str(fit), "/images/fdt-1", "compression", "gzip"], check=True)
        with self.assertRaisesRegex(ValueError, "uncompressed"):
            nor_probe.validate_fit(fit, self.root)


if __name__ == "__main__":
    unittest.main()

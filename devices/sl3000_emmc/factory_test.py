"""Public per-device Factory calibration test with the pinned generic fallback."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import nor_probe

HERE = Path(__file__).resolve().parent
FRAGMENT = "mt7981b-sl-3000-emmc-factory-test.dtsi"
MARKER_PATH = "etc/sl3000-factory-test.json"
PARTITION = nor_probe.PARTITIONS + "/partition@180000"
LAYOUT = PARTITION + "/nvmem-layout"
EEPROM = LAYOUT + "/eeprom@0"
PINS = "/soc/pinctrl@11d00000/sl3000-factory-pins"
NVMEM_POLICY = {"CONFIG_NVMEM": "y", "CONFIG_NVMEM_LAYOUTS": "y"}


def enabled():
    value = os.environ.get("SL3000_FACTORY_TEST", "false")
    if value not in ("true", "false"):
        raise ValueError("SL3000_FACTORY_TEST must be true or false")
    return value == "true"


def preflight(publish_release=False):
    import rf_test
    if not enabled():
        return
    if rf_test.enabled() or nor_probe.enabled():
        raise ValueError("Public Factory test cannot be combined with private RF/NOR profiles")
    if publish_release:
        raise ValueError("Public Factory test is artifact-only; do not publish a Release yet")
    if any(os.environ.get(name) for name in (rf_test.CALIBRATION_ENV, rf_test.PASSPHRASE_ENV)):
        raise ValueError("Public Factory test must not receive private calibration or encryption secrets")


def marker():
    return (json.dumps({
        "profile": "public-factory-eeprom-test",
        "hardware_validated": False,
        "mtd_label": "factory",
        "factory_offset": "0x180000",
        "factory_size": "0x200000",
        "eeprom_offset": 0,
        "eeprom_size": 4096,
        "nor_write_access": False,
        "wifi_calibration_source": "per-device-factory-nvmem",
        "fallback": "pinned-public-generic-eeprom-via-unmodified-mt76",
        "contains_private_calibration": False,
        "warning": "Matching hardware/layout only; generic fallback may have poor Wi-Fi coverage",
    }, indent=2) + "\n").encode()


def prepare(tree):
    preflight()
    if not enabled():
        return
    import rf_test
    for name in (rf_test.FIRMWARE_PATH, rf_test.MARKER_PATH, nor_probe.MARKER_PATH):
        path = tree / "files" / name
        if path.exists() or path.is_symlink():
            raise ValueError("Public Factory source tree contains an unexpected private overlay")
    dts_directory = tree / "target/linux/mediatek/dts"
    dts = dts_directory / "mt7981b-sl-3000-emmc.dts"
    if FRAGMENT in dts.read_text():
        raise ValueError("Factory test already prepared")
    shutil.copy2(HERE / "files" / FRAGMENT, dts_directory / FRAGMENT)
    dts.write_text(dts.read_text() + f'\n#include "{FRAGMENT}"\n')
    nor_probe.configure_kernel(tree, NVMEM_POLICY)
    path = tree / "files" / MARKER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(marker())


def validate_kernel(text):
    nor_probe.validate_kernel(text)
    values = dict(line.split("=", 1) for line in text.splitlines()
                  if line.startswith("CONFIG_") and "=" in line)
    if any(values.get(key) != value for key, value in NVMEM_POLICY.items()):
        raise ValueError("Factory test kernel lacks built-in NVMEM support")


def validate_dtb(path):
    get = nor_probe.fdt_get
    expected = [
        ("/", "compatible", "s", "sl,3000-emmc mediatek,mt7981"),
        ("/chosen", "bootargs", "s", "root=PARTLABEL=rootfs rootwait"),
        (nor_probe.SPI, "status", "s", "okay"),
        (nor_probe.FLASH, "compatible", "s", "jedec,spi-nor"),
        (nor_probe.FLASH, "reg", "x", "0"),
        (nor_probe.FLASH, "spi-max-frequency", "u", "10000000"),
        (nor_probe.FLASH, "spi-rx-bus-width", "u", "1"),
        (nor_probe.FLASH, "spi-tx-bus-width", "u", "1"),
        (nor_probe.PARTITIONS, "compatible", "s", "fixed-partitions"),
        (PARTITION, "label", "s", "factory"),
        (PARTITION, "reg", "x", "180000 200000"),
        (LAYOUT, "compatible", "s", "fixed-layout"),
        (EEPROM, "reg", "x", "0 1000"),
        (nor_probe.WIFI, "status", "s", "okay"),
        (nor_probe.WIFI, "nvmem-cell-names", "s", "eeprom"),
        (PINS + "/mux", "function", "s", "spi"),
        (PINS + "/mux", "groups", "s", "spi2 spi2_wp_hold"),
    ]
    for node in (nor_probe.PARTITIONS, LAYOUT):
        expected += [(node, "#address-cells", "u", "1"), (node, "#size-cells", "u", "1")]
    for node, prop, kind, value in expected:
        if get(path, node, prop, kind) != value:
            raise ValueError(f"Unexpected Factory device-tree field: {node}/{prop}")
    for node, children in ((nor_probe.SPI, ["flash@0"]), (nor_probe.FLASH, ["partitions"]),
                           (nor_probe.PARTITIONS, ["partition@180000"]),
                           (PARTITION, ["nvmem-layout"]), (LAYOUT, ["eeprom@0"]), (EEPROM, [])):
        if get(path, node, listing="l").split() != children:
            raise ValueError("Factory test must expose only the read-only Factory partition and EEPROM cell")
    if "read-only" not in get(path, PARTITION, listing="p").split():
        raise ValueError("Factory partition is writable")
    forbidden = {"mediatek,mtd-eeprom", "mediatek,eeprom-data"}
    if forbidden.intersection(get(path, nor_probe.WIFI, listing="p").split()):
        raise ValueError("Factory NVMEM must not be overridden by another EEPROM source")
    if get(path, nor_probe.WIFI, "nvmem-cells", "x") != get(path, EEPROM, "phandle", "x"):
        raise ValueError("Wi-Fi EEPROM cell is not connected to Factory")
    if get(path, nor_probe.SPI, "pinctrl-0", "x") != get(path, PINS, "phandle", "x"):
        raise ValueError("Factory SPI pins are not connected")


def validate_fit(path, scratch):
    nor_probe.validate_fit(path, scratch, validator=validate_dtb)


def validate_files(read_file, names):
    import rf_test
    if {rf_test.MARKER_PATH, nor_probe.MARKER_PATH}.intersection(names):
        raise ValueError("Private calibration profile found in public Factory image")
    lock = json.loads((HERE / "sources.lock.json").read_text())
    eeprom = read_file(rf_test.FIRMWARE_PATH)
    if len(eeprom) != 4096 or hashlib.sha256(eeprom).hexdigest() != lock["mt76_eeprom"]["sha256"]:
        raise ValueError("Public Factory fallback is not the pinned public generic EEPROM")
    if read_file(MARKER_PATH) != marker():
        raise ValueError("Public Factory marker missing or changed")


def validate_initramfs(path, scratch):
    get = nor_probe.fdt_get
    images = get(path, "/images", listing="l").split()
    ramdisks = {get(path, "/configurations/" + config, "ramdisk")
                for config in get(path, "/configurations", listing="l").split()}
    if not ramdisks:
        raise ValueError("Public Factory FIT has no inspectable initramfs")
    for number, ramdisk in enumerate(sorted(ramdisks)):
        node = "/images/" + ramdisk
        if get(path, node, "type") != "ramdisk":
            raise ValueError("Unexpected Factory initramfs image type")
        archive = scratch / f"factory-initramfs-{number}.cpio"
        subprocess.run(["dumpimage", "-T", "flat_dt", "-p", str(images.index(ramdisk)),
                        "-o", str(archive), str(path)], check=True, stdout=subprocess.DEVNULL)
        names = subprocess.check_output(["bsdtar", "-tf", str(archive)], text=True).splitlines()
        paths = {name.removeprefix("./"): name for name in names}
        if len(paths) != len(names):
            raise ValueError("Duplicate paths in Factory initramfs")
        def read_file(name):
            return subprocess.check_output(["bsdtar", "-xOf", str(archive), paths[name]])
        validate_files(read_file, paths)
        from verify import validate_network_files
        validate_network_files(read_file)

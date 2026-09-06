"""Opt-in, private NOR inspection without changing the Wi-Fi EEPROM source."""

import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
FRAGMENT = "mt7981b-sl-3000-emmc-nor-probe.dtsi"
MARKER_PATH = "etc/sl3000-nor-probe.json"
SPI = "/soc/spi@11009000"
FLASH = SPI + "/flash@0"
PARTITIONS = FLASH + "/partitions"
PARTITION = PARTITIONS + "/partition@0"
WIFI = "/soc/wifi@18000000"
PINS = "/soc/pinctrl@11d00000/sl3000-nor-probe-pins"
KERNEL_POLICY = {
    "CONFIG_MTD_PARTITIONED_MASTER": "n",
    "CONFIG_MTD_SPI_NOR_SWP_DISABLE": "n",
    "CONFIG_MTD_SPI_NOR_SWP_DISABLE_ON_VOLATILE": "n",
    "CONFIG_MTD_SPI_NOR_SWP_KEEP": "y",
}


def enabled():
    value = os.environ.get("SL3000_NOR_PROBE", "false")
    if value not in ("true", "false"):
        raise ValueError("SL3000_NOR_PROBE must be true or false")
    return value == "true"


def require_private():
    from rf_test import enabled as private_enabled
    if enabled() and not private_enabled():
        raise ValueError("NOR probe requires the private same-router RF profile")


def marker():
    return (json.dumps({
        "profile": "private-read-only-nor-probe",
        "hardware_validated": False,
        "mtd_label": "sl3000-nor-probe",
        "expected_nor_bytes": 32 * 1024**2,
        "nor_write_access": False,
        "wifi_calibration_source": "unchanged-private-runtime-eeprom",
        "factory_layout_verified": False,
    }, indent=2) + "\n").encode()


def prepare(tree):
    require_private()
    if not enabled():
        return
    dts_directory = tree / "target/linux/mediatek/dts"
    dts = dts_directory / "mt7981b-sl-3000-emmc.dts"
    if FRAGMENT in dts.read_text():
        raise ValueError("NOR probe already prepared")
    shutil.copy2(HERE / "files" / FRAGMENT, dts_directory / FRAGMENT)
    dts.write_text(dts.read_text() + f'\n#include "{FRAGMENT}"\n')
    config = tree / "target/linux/mediatek/filogic/config-6.12"
    lines = [line for line in config.read_text().splitlines()
             if not any(line.startswith(symbol + "=") or line == f"# {symbol} is not set"
                        for symbol in KERNEL_POLICY)]
    lines += [f"{symbol}=y" if value == "y" else f"# {symbol} is not set"
              for symbol, value in KERNEL_POLICY.items()]
    config.write_text("\n".join(lines) + "\n")
    path = tree / "files" / MARKER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(marker())


def validate_kernel(text):
    values = dict(line.split("=", 1) for line in text.splitlines()
                  if line.startswith("CONFIG_") and "=" in line)
    required = {"CONFIG_MTD": "y", "CONFIG_MTD_SPI_NOR": "y",
                "CONFIG_SPI": "y", "CONFIG_SPI_MT65XX": "y", **KERNEL_POLICY}
    if any(values.get(key, "n") != value for key, value in required.items()):
        raise ValueError("NOR probe kernel lacks read-only access or write-protection policy")


def fdt_get(path, node, prop=None, kind="s", listing=None):
    args = ["fdtget", "-" + listing] if listing else ["fdtget", "-t", kind]
    args += [str(path), node]
    if prop is not None:
        args.append(prop)
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()
    except subprocess.CalledProcessError as error:
        raise ValueError(f"Cannot inspect required device-tree field: {node}/{prop or ''}") from error


def validate_dtb(path):
    expected = [
        ("/", "compatible", "s", "sl,3000-emmc mediatek,mt7981"),
        ("/chosen", "bootargs", "s", "root=PARTLABEL=rootfs rootwait"),
        (SPI, "status", "s", "okay"),
        (FLASH, "compatible", "s", "jedec,spi-nor"),
        (FLASH, "reg", "x", "0"),
        (FLASH, "spi-max-frequency", "u", "10000000"),
        (FLASH, "spi-rx-bus-width", "u", "1"),
        (FLASH, "spi-tx-bus-width", "u", "1"),
        (PARTITIONS, "compatible", "s", "fixed-partitions"),
        (PARTITION, "label", "s", "sl3000-nor-probe"),
        (PARTITION, "reg", "x", "0 2000000"),
        (WIFI, "status", "s", "okay"),
        (PINS + "/mux", "function", "s", "spi"),
        (PINS + "/mux", "groups", "s", "spi2 spi2_wp_hold"),
    ]
    for node, prop, kind, value in expected:
        if fdt_get(path, node, prop, kind) != value:
            raise ValueError(f"Unexpected NOR probe device-tree field: {node}/{prop}")
    for node, children in ((SPI, ["flash@0"]), (FLASH, ["partitions"]),
                           (PARTITIONS, ["partition@0"]), (PARTITION, [])):
        if fdt_get(path, node, listing="l").split() != children:
            raise ValueError("NOR probe must expose only one read-only raw partition")
    if "read-only" not in fdt_get(path, PARTITION, listing="p").split():
        raise ValueError("NOR probe partition is writable")
    forbidden = {"nvmem-cells", "nvmem-cell-names", "mediatek,mtd-eeprom", "mediatek,eeprom-data"}
    if forbidden.intersection(fdt_get(path, WIFI, listing="p").split()):
        raise ValueError("NOR probe must not switch the Wi-Fi calibration source")
    if fdt_get(path, SPI, "pinctrl-0", "x") != fdt_get(path, PINS, "phandle", "x"):
        raise ValueError("NOR probe SPI pins are not connected")


def validate_fit(path, scratch):
    configs = fdt_get(path, "/configurations", listing="l").split()
    if not configs or fdt_get(path, "/configurations", "default") not in configs:
        raise ValueError("NOR probe FIT has no valid default configuration")
    for index, config in enumerate(configs):
        image = fdt_get(path, "/configurations/" + config, "fdt")
        node = "/images/" + image
        if fdt_get(path, node, "type") != "flat_dt" or fdt_get(path, node, "compression") != "none":
            raise ValueError("NOR probe requires an inspectable uncompressed FIT device tree")
        dtb = scratch / f"nor-probe-{index}.dtb"
        dtb.write_bytes(bytes(int(word, 16) for word in fdt_get(path, node, "data", "bx").split()))
        validate_dtb(dtb)

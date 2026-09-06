#!/usr/bin/env python3
"""Fail closed on config drift, incomplete images and unsafe release contents."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
BOARD = "sl,3000-emmc"
PROFILE = "sl_3000-emmc"
REQUIRED_PACKAGES = (
    "luci", "luci-ssl", "dnsmasq-full", "firewall4", "tailscale",
    "luci-app-tailscale-community", "luci-app-passwall", "luci-app-openclash", "ruby", "ruby-yaml",
    "mihomo", "xray-core", "sing-box", "chinadns-ng", "dns2socks", "ipt2socks",
    "kmod-nft-socket", "kmod-nft-tproxy", "kmod-nft-nat", "kmod-tun",
    "luci-app-upnp", "miniupnpd-nftables", "luci-app-wol", "etherwake",
    "kmod-mt7915e", "kmod-mt7981-firmware", "mt7981-wo-firmware",
    "sl3000-default-eeprom", "kmod-mmc", "kmod-usb3", "kmod-fs-ext4",
    "kmod-fs-f2fs", "block-mount", "f2fsck", "mkf2fs", "ca-bundle", "ip-full",
)


def validate_config(text):
    settings = dict(line.split("=", 1) for line in text.splitlines() if line.startswith("CONFIG_") and "=" in line)
    devices = [key for key, value in settings.items() if key.startswith("CONFIG_TARGET_") and "_DEVICE_" in key and value == "y"]
    if devices != [f"CONFIG_TARGET_mediatek_filogic_DEVICE_{PROFILE}"]:
        raise ValueError("Exactly the SL-3000 eMMC target must be selected")
    required = [f"CONFIG_PACKAGE_{package}" for package in REQUIRED_PACKAGES]
    required += ["CONFIG_TARGET_mediatek", "CONFIG_TARGET_mediatek_filogic", "CONFIG_TARGET_ROOTFS_SQUASHFS",
                 "CONFIG_TARGET_ROOTFS_INITRAMFS", "CONFIG_PACKAGE_dnsmasq_full_nftset",
                 "CONFIG_PACKAGE_luci-app-passwall_Nftables_Transparent_Proxy"]
    missing = [key for key in required if settings.get(key) != "y"]
    if missing:
        raise ValueError("Required built-ins missing: " + ", ".join(missing))
    if settings.get("CONFIG_RUBY_ENABLE_YJIT") in ("y", "m"):
        raise ValueError("Ruby YJIT must stay disabled to avoid its Rust/LLVM build dependency")
    forbidden = ["CONFIG_TARGET_ALL_PROFILES", "CONFIG_TARGET_MULTI_PROFILE", "CONFIG_ALL_KMODS",
                 "CONFIG_PACKAGE_dnsmasq", "CONFIG_PACKAGE_miniupnpd-iptables",
                 "CONFIG_PACKAGE_luci-app-attendedsysupgrade", "CONFIG_PACKAGE_owut"]
    forbidden += [key for key in settings if key.startswith(("CONFIG_PACKAGE_uboot-", "CONFIG_PACKAGE_arm-trusted-firmware-"))]
    selected_forbidden = [key for key in forbidden if settings.get(key) in ("y", "m")]
    if selected_forbidden:
        raise ValueError("Conflicting package, automatic upgrader or bootloader selected: " + ", ".join(selected_forbidden))


def validate_manifest(text):
    packages = {line.split(" - ", 1)[0] for line in text.splitlines() if " - " in line}
    missing = set(REQUIRED_PACKAGES) - packages
    if missing:
        raise ValueError("Manifest missing: " + ", ".join(sorted(missing)))


def validate_metadata(metadata):
    if metadata.get("supported_devices") != [BOARD]:
        raise ValueError("Image metadata does not exclusively support sl,3000-emmc")


def validate_payload_sizes(kernel, root):
    if not 0 < kernel <= 32 * 1024**2 or not 0 < root <= 2000 * 1024**2 - 65536:
        raise ValueError("Image exceeds current kernel/rootfs partitions or has an empty payload")


def validate_tar(path):
    prefix = f"sysupgrade-{PROFILE}"
    allowed = {prefix, *(f"{prefix}/{name}" for name in ("CONTROL", "kernel", "root"))}
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = [m.name.rstrip("/") for m in members]
        if len(set(names)) != len(names) or set(names) - allowed:
            raise ValueError("Unexpected or duplicate sysupgrade tar member")
        if any(not (m.isfile() or (m.isdir() and m.name.rstrip("/") == prefix)) for m in members):
            raise ValueError("Links and special files are forbidden in sysupgrade tar")
        if not {f"{prefix}/{name}" for name in ("CONTROL", "kernel", "root")} <= set(names):
            raise ValueError("Sysupgrade tar is incomplete")
        kernel = archive.getmember(f"{prefix}/kernel")
        root = archive.getmember(f"{prefix}/root")
        validate_payload_sizes(kernel.size, root.size)
        if archive.extractfile(kernel).read(4) != b"\xd0\x0d\xfe\xed":
            raise ValueError("Kernel is not FIT")
        if archive.extractfile(root).read(4) != b"hsqs":
            raise ValueError("Root is not SquashFS")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def one(paths, label):
    paths = list(paths)
    if len(paths) != 1:
        raise ValueError(f"Expected one {label}, found {len(paths)}")
    return paths[0]


def root_file(root, name):
    return subprocess.check_output(["unsquashfs", "-cat", str(root), name], stderr=subprocess.DEVNULL)


def verify_rootfs(image, scratch, password):
    from inject_defaults import shell_assignment
    root = scratch / "root.squashfs"
    with tarfile.open(image) as archive, root.open("wb") as stream:
        shutil.copyfileobj(archive.extractfile(f"sysupgrade-{PROFILE}/root"), stream)
    expected = (HERE / "firstboot.sh").read_text().replace("# WIFI_PASSWORD_INJECTED_HERE", shell_assignment(password)).encode()
    if root_file(root, "etc/uci-defaults/99-sl3000-setup") != expected:
        raise ValueError("Firstboot settings are missing or differ from the validated template")
    core = root_file(root, "etc/openclash/core/clash_meta")
    if core[:6] != b"\x7fELF\x02\x01" or core[18:20] != b"\xb7\x00":
        raise ValueError("Mihomo is not a little-endian AArch64 ELF executable")
    eeprom = root_file(root, "lib/firmware/mediatek/mt7981_eeprom_mt7976_dbdc.bin")
    lock = json.loads((HERE / "sources.lock.json").read_text())
    import rf_test
    if rf_test.enabled():
        expected_eeprom = rf_test.calibration()
        if eeprom != expected_eeprom:
            raise ValueError("Private runtime EEPROM was not embedded exactly")
        if root_file(root, rf_test.MARKER_PATH) != rf_test.marker(expected_eeprom):
            raise ValueError("Private RF test marker missing or changed")
    elif hashlib.sha256(eeprom).hexdigest() != lock["mt76_eeprom"]["sha256"]:
        raise ValueError("Default EEPROM missing or changed")
    import nor_probe
    nor_probe.require_private()
    if nor_probe.enabled() and root_file(root, nor_probe.MARKER_PATH) != nor_probe.marker():
        raise ValueError("NOR probe marker missing or changed")
    shadow = root_file(root, "etc/shadow").decode()
    root_password = next(line.split(":")[1] for line in shadow.splitlines() if line.startswith("root:"))
    if root_password not in ("", "*", "!"):
        raise ValueError("A root password must not be embedded")
    listing = subprocess.check_output(["unsquashfs", "-ll", str(root)], text=True)
    if re.search(r"(?:tailscaled\.state|id_rsa|id_ed25519|authorized_keys|dropbear_\w+_host_key)(?:\s|$)", listing):
        raise ValueError("Firmware contains account or host identity state")


def artifacts(tree, destination):
    from inject_defaults import validate_password
    password = os.environ.get("DEFAULT_WIFI_PASSWORD", "")
    validate_password(password)
    config = (tree / ".config").read_text()
    validate_config(config)
    # The pinned Rust package installs into the target-specific host directory.
    rust_compilers = [tree / "staging_dir/host/bin/rustc", tree / "staging_dir/hostpkg/bin/rustc",
                      *tree.glob("staging_dir/target-*/host/bin/rustc")]
    if any(path.exists() or path.is_symlink() for path in rust_compilers):
        raise ValueError("Unexpected Rust host toolchain; check the resolved build dependencies")
    target = tree / "bin/targets/mediatek/filogic"
    sysupgrade = one(target.glob(f"*-{PROFILE}-squashfs-sysupgrade.bin"), "sysupgrade")
    initramfs = one(target.glob(f"*-{PROFILE}-initramfs.itb"), "initramfs")
    manifest = one(target.glob(f"*-{PROFILE}.manifest"), "manifest")
    validate_manifest(manifest.read_text())
    validate_tar(sysupgrade)
    with initramfs.open("rb") as stream:
        if stream.read(4) != b"\xd0\x0d\xfe\xed":
            raise ValueError("Initramfs image is not FIT")
    fwtool = tree / "staging_dir/host/bin/fwtool"
    with tempfile.TemporaryDirectory(prefix="sl3000-verify-") as directory:
        scratch = Path(directory)
        metadata_file = scratch / "metadata.json"
        subprocess.run([str(fwtool), "-i", str(metadata_file), str(sysupgrade)], check=True)
        metadata = json.loads(metadata_file.read_text())
        validate_metadata(metadata)
        verify_rootfs(sysupgrade, scratch, password)
        import nor_probe
        nor_probe.require_private()
        if nor_probe.enabled():
            kernel = scratch / "kernel.fit"
            with tarfile.open(sysupgrade) as archive, kernel.open("wb") as stream:
                shutil.copyfileobj(archive.extractfile(f"sysupgrade-{PROFILE}/kernel"), stream)
            nor_probe.validate_fit(kernel, scratch)
            nor_probe.validate_fit(initramfs, scratch)
    kernel_config = one(tree.glob("build_dir/target-*/linux-mediatek_filogic/linux-6.12*/.config"), "6.12 kernel config")
    if not all(f"{symbol}=y" in kernel_config.read_text() for symbol in ("CONFIG_MMC", "CONFIG_MMC_BLOCK", "CONFIG_MMC_MTK")):
        raise ValueError("Kernel lacks built-in eMMC support")
    if nor_probe.enabled():
        nor_probe.validate_kernel(kernel_config.read_text())
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Release directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    export = {
        sysupgrade.name: sysupgrade, initramfs.name: initramfs, manifest.name: manifest,
        "openwrt.config": tree / ".config", "kernel.config": kernel_config,
        "sources.lock.json": HERE / "sources.lock.json", "README.md": HERE / "README.md",
    }
    for name, source in export.items():
        if source.is_symlink() or re.search(r"gpt|bl2|preloader|fip|u-boot|uboot", name, re.I):
            raise ValueError("Forbidden artifact")
        if source.suffix not in (".bin", ".itb") and password.encode() in source.read_bytes():
            raise ValueError("Setup password leaked into an auxiliary artifact")
        shutil.copy2(source, destination / name)
    provenance = {
        "hardware_validated": False, "status": "prerelease",
        "recipe_commit": os.environ.get("GITHUB_SHA", "local-uncommitted"),
        "workflow_run": os.environ.get("GITHUB_RUN_ID", "local"),
        "supported_devices": metadata["supported_devices"],
        "wifi_password_is_public_temporary": True,
    }
    from rf_test import enabled
    provenance["wifi_profile"] = "private-runtime-eeprom-ab-test" if enabled() else "generic-bootstrap"
    provenance["nor_profile"] = "read-only-probe" if nor_probe.enabled() else "disabled"
    (destination / "build-info.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (destination / "sha256sums").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in sorted(destination.iterdir())))
    print("Validated SL-3000 images, packages, defaults and allowlisted artifacts. Hardware validation still required.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("config", "artifacts"))
    parser.add_argument("openwrt", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "config":
            validate_config((args.openwrt / ".config").read_text())
            print("SL-3000 target and required built-in packages verified.")
        else:
            if not args.output:
                raise ValueError("--output is required for artifacts")
            artifacts(args.openwrt.resolve(), args.output.resolve())
    except (ValueError, tarfile.TarError) as error:
        parser.exit(1, str(error) + "\n")

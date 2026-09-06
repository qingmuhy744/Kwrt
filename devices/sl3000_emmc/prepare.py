#!/usr/bin/env python3
"""Prepare a dedicated, pinned OpenWrt source tree, not the Kwrt common build."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
PROXY_PACKAGES = ("chinadns-ng", "dns2socks", "ipt2socks", "microsocks", "tcping", "sing-box", "xray-core")


def validate_lock(lock):
    if lock.get("schema") != 1:
        raise ValueError("Unsupported source lock schema")
    for source in [lock["openwrt"], *lock["feeds"].values()]:
        if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
            raise ValueError("Every repository must use a full commit SHA")
        if not re.fullmatch(r"https://github.com/[\w.-]+/[\w.-]+\.git", source["url"]):
            raise ValueError("Unexpected repository URL")
    for name in ("mihomo", "mt76_eeprom"):
        if not re.fullmatch(r"[0-9a-f]{64}", lock[name]["sha256"]):
            raise ValueError("Binary download must have a SHA-256 digest")


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True)


def revision(path):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(f"Source context changed: {path.name}")
    path.write_text(text.replace(old, new))


def validate_package_sources(tree):
    for package in PROXY_PACKAGES:
        links = [feed / package for feed in (tree / "package/feeds").iterdir()
                 if (feed / package).exists() or (feed / package).is_symlink()]
        expected = tree / "feeds/passwall_packages" / package
        if len(links) != 1 or links[0].resolve() != expected.resolve():
            raise ValueError(f"Unexpected feed provider for {package}")


def prepare_network(base):
    if '\tsl,3000-emmc)\n' in (base / "etc/board.d/02_network").read_text():
        raise ValueError("Source context changed: SL-3000 network case already exists")
    anchor = 'mediatek_setup_interfaces()\n{\n\tlocal board="$1"\n\n\tcase $board in\n'
    replace_once(base / "etc/board.d/02_network", anchor, anchor + '''\tsl,3000-emmc)
\t\tucidef_set_interfaces_lan_wan "lan1 lan2 lan3" wan
\t\t;;
''')
    for source, destination in (
        ("03_sl3000-network", "etc/board.d/03_sl3000-network"),
        ("98-sl3000-ports", "etc/uci-defaults/98-sl3000-ports"),
        ("sl3000-ports.uc", "usr/libexec/sl3000-ports.uc"),
    ):
        path = base / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "files" / source, path)
        path.chmod(0o755 if source != "sl3000-ports.uc" else 0o644)


def prepare(tree):
    import factory_test
    factory_test.preflight()
    lock = json.loads((HERE / "sources.lock.json").read_text())
    validate_lock(lock)
    tree.mkdir(parents=True, exist_ok=True)
    if not (tree / ".git").exists():
        if set(p.name for p in tree.iterdir()) - {"dl", ".ccache"}:
            raise ValueError("Refusing to initialize a nonempty source directory")
        run("git", "init", str(tree), cwd=tree.parent)
        run("git", "remote", "add", "origin", lock["openwrt"]["url"], cwd=tree)
        run("git", "fetch", "--depth=1", "origin", lock["openwrt"]["commit"], cwd=tree)
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=tree)
    if revision(tree) != lock["openwrt"]["commit"]:
        raise ValueError("OpenWrt commit does not match lock")
    run("git", "diff", "--exit-code", "--quiet", "HEAD", cwd=tree)

    image = tree / "target/linux/mediatek/image/filogic.mk"
    if "Device/sl_3000-emmc" in image.read_text():
        raise ValueError("Source tree already prepared; use a fresh checkout")
    image.write_text(image.read_text() + (HERE / "image.mk").read_text())
    shutil.copy2(HERE / "files/mt7981b-sl-3000-emmc.dts", tree / "target/linux/mediatek/dts/")
    import nor_probe
    nor_probe.prepare(tree)
    factory_test.prepare(tree)
    base = tree / "target/linux/mediatek/filogic/base-files"
    prepare_network(base)
    shutil.copy2(HERE / "files/sl3000-upgrade.sh", base / "lib/upgrade/")
    platform = base / "lib/upgrade/platform.sh"
    replace_once(platform, 'REQUIRE_IMAGE_METADATA=1', 'REQUIRE_IMAGE_METADATA=1\n. /lib/upgrade/sl3000-upgrade.sh')
    replace_once(platform, 'platform_do_upgrade() {', '''platform_do_upgrade() {
    if [ "$(board_name)" = "sl,3000-emmc" ]; then
        sl3000_check_image "$1" || return 1
        CI_ROOTDEV=mmcblk0
        CI_KERNPART=kernel
        CI_ROOTPART=rootfs
        CI_DATAPART=
        CI_DTBPART=
        unset EMMC_KERN_DEV EMMC_ROOT_DEV EMMC_DATA_DEV EMMC_DTB_DEV
        emmc_do_upgrade "$1"
        return $?
    fi''')
    replace_once(platform, 'platform_check_image() {', '''platform_check_image() {
    if [ "$(board_name)" = "sl,3000-emmc" ]; then
        sl3000_check_image "$1"
        return $?
    fi''')
    replace_once(platform, 'platform_copy_config() {', '''platform_copy_config() {
    if [ "$(board_name)" = "sl,3000-emmc" ]; then
        emmc_copy_config
        return $?
    fi''')
    shutil.copytree(HERE / "packages", tree / "package/sl3000-local")
    # Package recipes and provenance must agree on immutable binary downloads.
    for name, recipe in (("mihomo", "mihomo"), ("mt76_eeprom", "sl3000-default-eeprom")):
        makefile = (tree / f"package/sl3000-local/{recipe}/Makefile").read_text()
        if lock[name]["sha256"] not in makefile:
            raise ValueError(f"Package checksum differs from lock: {name}")
    (tree / "feeds.conf").write_text("".join(
        f"src-git {name} {source['url']}^{source['commit']}\n"
        for name, source in lock["feeds"].items()
    ))
    run("./scripts/feeds", "update", "-a", cwd=tree)
    for name, source in lock["feeds"].items():
        if revision(tree / "feeds" / name) != source["commit"]:
            raise ValueError(f"Feed commit mismatch: {name}")
    run("./scripts/feeds", "install", "-a", cwd=tree)
    # Only these proxy packages override the release feed; toolchain stays official.
    # -f only replaces core recipes, not an already installed feed recipe.
    run("./scripts/feeds", "uninstall", *PROXY_PACKAGES, cwd=tree)
    run("./scripts/feeds", "install", "-p", "passwall_packages", *PROXY_PACKAGES, cwd=tree)
    validate_package_sources(tree)
    run("./scripts/feeds", "install", "-f", "-p", "passwall", "luci-app-passwall", cwd=tree)
    run("./scripts/feeds", "install", "-f", "-p", "openclash", "luci-app-openclash", cwd=tree)
    shutil.copy2(HERE / ".config", tree / ".config")
    shutil.copy2(HERE / "sources.lock.json", tree / "sl3000-sources.lock.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("openwrt", type=Path)
    args = parser.parse_args()
    prepare(args.openwrt.resolve())

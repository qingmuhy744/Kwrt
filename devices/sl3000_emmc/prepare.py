#!/usr/bin/env python3
"""Prepare a dedicated, pinned OpenWrt source tree, not the Kwrt common build."""

import argparse
import hashlib
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
    paths = []
    for name, update in lock.get("package_updates", {}).items():
        source, path = update["source"], update["path"]
        if source not in ("openwrt", *lock["feeds"]):
            raise ValueError("Unknown package update source")
        if not path or any(not re.fullmatch(r"[\w+-][\w+.-]*", part) for part in path.split("/")):
            raise ValueError("Invalid package update path")
        if source == "openwrt" and not path.startswith("package/"):
            raise ValueError("Only package recipes may override the OpenWrt baseline")
        if any(source == other and (path == prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/"))
               for other, prefix in paths):
            raise ValueError("Overlapping package update paths")
        paths.append((source, path))
        if not re.fullmatch(r"[0-9a-f]{40}", update["commit"]):
            raise ValueError("Package updates must use full commit SHAs")
        if "release" in update and not re.fullmatch(r"[1-9][0-9]*", update["release"]):
            raise ValueError("Invalid package release override")
        if "version" in update or "sha256" in update:
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", update.get("version", "")) or not re.fullmatch(
                    r"[0-9a-f]{64}", update.get("sha256", "")):
                raise ValueError("Source version overrides require a stable version and SHA-256")
        if "host_version" in update and (name != "golang" or source != "packages" or
                path != "lang/golang/golang1.26" or not re.fullmatch(r"1\.26\.[0-9]+", update["host_version"])):
            raise ValueError("Unsupported host toolchain update")
        if (not update.get("packages") and "host_version" not in update) or any(not re.fullmatch(r"[A-Za-z0-9+_.-]+", package)
                or not re.fullmatch(r"[^\s]+-r[0-9]+", version) for package, version in update["packages"].items()):
            raise ValueError("Package updates must declare expected APK versions")
        patch_names = set()
        for patch in update.get("patches", []):
            if not re.fullmatch(re.escape(name) + r"/[A-Za-z0-9_.-]+\.patch", patch["file"]):
                raise ValueError("Invalid package patch path")
            if patch["file"] in patch_names:
                raise ValueError("Duplicate package patch")
            patch_names.add(patch["file"])
            if not re.fullmatch(r"[0-9a-f]{40}", patch["commit"]) or not re.fullmatch(r"[0-9a-f]{64}", patch["sha256"]):
                raise ValueError("Package patches must pin their upstream commit and checksum")
    from hardening import FIXES
    if not isinstance(lock.get("hardening", []), list) or any(item not in FIXES for item in lock.get("hardening", [])):
        raise ValueError("Unsupported firmware hardening fix")


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True)


def revision(path):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(f"Source context changed: {path.name}")
    path.write_text(text.replace(old, new))


def updated_makefile(data, update):
    for key, variable, pattern in (("release", "PKG_RELEASE", rb"[0-9]+"),
                                   ("version", "PKG_VERSION", rb"[0-9]+(?:\.[0-9]+)+"),
                                   ("sha256", "PKG_HASH", rb"[0-9a-f]{64}")):
        if key not in update:
            continue
        data, count = re.subn(rb"(?m)^" + variable.encode() + rb":=" + pattern + rb"$",
                             variable.encode() + b":=" + update[key].encode(), data)
        if count != 1:
            raise ValueError(f"Package {key} source context changed")
    return data


def package_patch(patch):
    data = (HERE / "patches" / patch["file"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != patch["sha256"]:
        raise ValueError("Package patch checksum differs from lock")
    return data


def apply_package_updates(tree, lock, source):
    root = tree if source == "openwrt" else tree / "feeds" / source
    for update in lock.get("package_updates", {}).values():
        if update["source"] != source:
            continue
        run("git", "fetch", "--depth=1", "origin", update["commit"], cwd=root)
        run("git", "restore", "--source=" + update["commit"], "--staged", "--worktree", "--", update["path"], cwd=root)
        package = root / update["path"]
        makefile = package / "Makefile"
        makefile.write_bytes(updated_makefile(makefile.read_bytes(), update))
        for patch in update.get("patches", []):
            destination = package / "patches" / Path(patch["file"]).name
            destination.parent.mkdir(exist_ok=True)
            if destination.exists():
                raise ValueError("Package patch would overwrite upstream content")
            destination.write_bytes(package_patch(patch))


def verify_package_updates(tree, lock):
    """Check complete recipe contents, including removals and declared backports."""
    for name, update in lock.get("package_updates", {}).items():
        root = tree if update["source"] == "openwrt" else tree / "feeds" / update["source"]
        package = root / update["path"]
        listing = subprocess.check_output(
            ["git", "ls-tree", "-r", update["commit"], "--", update["path"]], cwd=root, text=True)
        expected = {}
        for line in listing.splitlines():
            metadata, path = line.split("\t", 1)
            mode, kind, digest = metadata.split()
            if kind != "blob" or mode not in ("100644", "100755"):
                raise ValueError("Unsupported package recipe entry")
            relative = Path(path).relative_to(update["path"]).as_posix()
            expected[relative] = (mode, digest)
        if "Makefile" not in expected:
            raise ValueError("Pinned package recipe lacks a Makefile")
        makefile = subprocess.check_output(["git", "show", update["commit"] + ":" + update["path"] + "/Makefile"], cwd=root)
        additions = {"Makefile": updated_makefile(makefile, update)}
        for patch in update.get("patches", []):
            relative = "patches/" + Path(patch["file"]).name
            if relative in expected:
                raise ValueError("Package patch conflicts with upstream content")
            additions[relative] = package_patch(patch)
        for path, data in additions.items():
            expected[path] = ("100644", hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest())
        actual = {path.relative_to(package).as_posix(): path for path in package.rglob("*")
                  if path.is_file() or path.is_symlink()}
        if actual.keys() != expected.keys():
            raise ValueError(f"Package recipe file set differs from lock: {name}")
        for path, (mode, digest) in expected.items():
            entry = actual[path]
            if entry.is_symlink() or bool(entry.stat().st_mode & 0o111) != (mode == "100755"):
                raise ValueError(f"Package recipe file mode differs from lock: {name}/{path}")
            data = entry.read_bytes()
            if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != digest:
                raise ValueError(f"Package recipe content differs from lock: {name}/{path}")
    return lock.get("package_updates", {})


def verify_build_tools(tree, lock):
    tools = {}
    for name, update in lock.get("package_updates", {}).items():
        if "host_version" not in update:
            continue
        compiler = tree / "staging_dir/hostpkg/lib/go-1.26/bin/go"
        output = subprocess.check_output([str(compiler), "version"], text=True).strip()
        if not output.startswith("go version go" + update["host_version"] + " "):
            raise ValueError("Go build toolchain version differs from lock")
        tools[name] = update["host_version"]
    return tools


def validate_updated_packages(text, lock):
    packages = dict(line.split(" - ", 1) for line in text.splitlines() if " - " in line)
    for update in lock.get("package_updates", {}).values():
        for package, version in update["packages"].items():
            if packages.get(package) != version:
                raise ValueError(f"Security update missing from manifest: {package} must be {version}")


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
    apply_package_updates(tree, lock, "openwrt")
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
        apply_package_updates(tree, lock, name)
        if any(update["source"] == name for update in lock.get("package_updates", {}).values()):
            run("./scripts/feeds", "update", "-i", name, cwd=tree)
    verify_package_updates(tree, lock)
    import hardening
    hardening.prepare(tree, lock)
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

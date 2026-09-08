#!/usr/bin/env python3
"""Bind a public build's package manifest to the firmware recipe inputs."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
PREFIX = "devices/sl3000_emmc/"


def recipe_input(name):
    return name in (".config", "sources.lock.json", "image.mk") or name.startswith(("files/", "packages/", "patches/")) or (
        "/" not in name and name.endswith(".py") and not name.startswith(("security_", "ci_")) and name != "verify.py")


def fingerprint(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def recipe_digest(root=HERE):
    entries = {}
    for path in root.rglob("*"):
        if not path.is_file() or not recipe_input(path.relative_to(root).as_posix()):
            continue
        data = path.read_bytes()
        entries[path.relative_to(root).as_posix()] = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    return fingerprint(entries)


def remote_digest(github, repository, recipe):
    tree = github.get(f"repos/{repository}/git/trees/{recipe}?recursive=1")
    if tree.get("truncated"):
        raise ValueError("Recipe tree is incomplete")
    entries = {item["path"][len(PREFIX):]: item["sha"] for item in tree["tree"]
               if item["type"] == "blob" and item["path"].startswith(PREFIX)
               and recipe_input(item["path"][len(PREFIX):])}
    if "sources.lock.json" not in entries or ".config" not in entries:
        raise ValueError("Recipe tree lacks build inputs")
    return fingerprint(entries)


def parse_manifest(text):
    packages = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9+_.-]*) - (\S+)", line)
        if not match or match[1] in packages:
            raise ValueError("Invalid or duplicate manifest package")
        packages[match[1]] = match[2]
    if not {"kernel", "base-files", "uhttpd", "luci-base"}.issubset(packages):
        raise ValueError("Incomplete SL3000 package manifest")
    return packages


def collect(directory, root=HERE, openwrt=None):
    manifests = list(directory.glob("*sl_3000-emmc.manifest"))
    if len(manifests) != 1:
        raise ValueError("Expected one verified SL3000 manifest")
    lock = json.loads((directory / "sources.lock.json").read_text())
    if lock != json.loads((root / "sources.lock.json").read_text()):
        raise ValueError("Build source lock differs from the recipe")
    provenance = json.loads((directory / "build-info.json").read_text())
    if "sl,3000-emmc" not in provenance["supported_devices"]:
        raise ValueError("Inventory is for another device")
    # Keep build flags only, never configuration strings or runtime settings.
    flags = dict(re.findall(r"^(CONFIG_[A-Za-z0-9_]+)=(y|n|m)$", (directory / "openwrt.config").read_text(), re.M))
    flags.update({key: "n" for key in re.findall(
        r"^# (CONFIG_[A-Za-z0-9_]+) is not set$", (directory / "openwrt.config").read_text(), re.M)})
    overrides = {}
    updates = {}
    hardening_fixes, build_tools = [], {}
    if openwrt:
        from prepare import verify_package_updates, validate_updated_packages, verify_build_tools
        import hardening
        updates = verify_package_updates(openwrt, lock)
        hardening_fixes = hardening.verify(
            lambda name: (openwrt / "feeds/luci/modules/luci-mod-system/root" / name).read_bytes(), lock)
        build_tools = verify_build_tools(openwrt, lock)
        validate_updated_packages(manifests[0].read_text(), lock)
        for name, tree in [("openwrt", openwrt), *[(name, openwrt / "feeds" / name) for name in lock["feeds"]]]:
            changed = subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=tree, text=True).splitlines()
            added = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=tree, text=True).splitlines()
            overrides[name] = sorted(set(changed + added))
    elif lock.get("package_updates") or lock.get("hardening"):
        raise ValueError("Package updates require verification against the build source tree")
    return {"schema": 1, "recipe_digest": recipe_digest(root), "lock": lock, "source_overrides": overrides,
            "verified_package_updates": updates,
            "verified_hardening": hardening_fixes, "build_tools": build_tools,
            "recipe_commit": provenance["recipe_commit"], "workflow_run": provenance["workflow_run"],
            "packages": parse_manifest(manifests[0].read_text()),
            "flags": {key: value for key, value in flags.items()
                      if key.startswith(("CONFIG_DRIVER_", "CONFIG_WPA_", "CONFIG_LIBCURL_", "CONFIG_MBEDTLS_"))}}


def validate(inventory, expected, lock):
    if inventory.get("schema") != 1 or inventory.get("recipe_digest") != expected or inventory.get("lock") != lock:
        return False
    if inventory.get("verified_package_updates", {}) != lock.get("package_updates", {}):
        return False
    if inventory.get("verified_hardening", []) != lock.get("hardening", []):
        return False
    for name, update in lock.get("package_updates", {}).items():
        if "host_version" in update and inventory.get("build_tools", {}).get(name) != update["host_version"]:
            return False
    packages = inventory.get("packages", {})
    manifest = "\n".join(f"{name} - {version}" for name, version in packages.items())
    parse_manifest(manifest)
    from prepare import validate_updated_packages
    validate_updated_packages(manifest, lock)
    if not re.fullmatch(r"[0-9a-f]{40}", inventory.get("recipe_commit", "")):
        raise ValueError("Invalid inventory provenance")
    if not str(inventory.get("workflow_run", "")).isdecimal():
        raise ValueError("Invalid inventory build run")
    return True


def load(github, repository, lock, previous, recipe=""):
    expected = remote_digest(github, repository, recipe) if recipe else recipe_digest()
    candidates = [previous.get("inventory", {})]
    bootstrap = HERE / "security-baseline.json"
    if bootstrap.exists():
        candidates.append(json.loads(bootstrap.read_text()))
    for candidate in candidates:
        if validate(candidate, expected, lock):
            return candidate
    # Future builds publish a small, public inventory separately from firmware.
    branch = github.get(f"repos/{repository}")["default_branch"]
    from urllib.parse import urlencode
    query = urlencode({"status": "success", "branch": branch, "per_page": 20})
    runs = github.get(f"repos/{repository}/actions/workflows/sl3000-emmc.yml/runs?{query}")
    for run in runs["workflow_runs"]:
        artifacts = github.get(f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100")
        name = f"sl3000-security-inventory-{run['id']}"
        if not any(item["name"] == name and not item["expired"] for item in artifacts["artifacts"]):
            continue
        with tempfile.TemporaryDirectory(prefix="sl3000-inventory-") as directory:
            result = subprocess.run(["gh", "run", "download", str(run["id"]), "--repo", repository,
                                     "--name", name, "--dir", directory], capture_output=True, timeout=60)
            if result.returncode:
                raise ValueError("Could not download the public package inventory")
            candidate = json.loads((Path(directory) / "security-inventory.json").read_text())
        if candidate.get("recipe_commit") != run["head_sha"] or str(candidate.get("workflow_run")) != str(run["id"]):
            raise ValueError("Package inventory provenance does not match its build")
        if validate(candidate, expected, lock):
            return candidate
    raise ValueError("No successful build inventory matches the current recipe; build it before assessing package exposure")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--openwrt", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(collect(args.directory, openwrt=args.openwrt), ensure_ascii=True, indent=2) + "\n")

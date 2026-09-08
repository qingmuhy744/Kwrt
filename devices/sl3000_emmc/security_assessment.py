"""Filter upstream evidence against a matching SL3000 build inventory."""

import base64
from fnmatch import fnmatchcase
import re
from urllib.parse import urlencode


# Paths are source recipe directories; selectors refer to output APK names,
# including ABI suffixes and mutually exclusive TLS variants.
COMPONENTS = {
    "uhttpd": ("openwrt", "package/network/services/uhttpd", ("uhttpd", "uhttpd-mod-*"), "openwrt/uhttpd"),
    "odhcpd": ("openwrt", "package/network/services/odhcpd", ("odhcpd", "odhcpd-ipv6only"), "openwrt/odhcpd"),
    "rpcd": ("openwrt", "package/system/rpcd", ("rpcd", "rpcd-mod-file", "rpcd-mod-iwinfo", "rpcd-mod-ucode", "rpcd-mod-rpcsys"), "openwrt/rpcd"),
    "ubus": ("openwrt", "package/system/ubus", ("ubus", "ubusd", "libubus*"), "openwrt/ubus"),
    "netifd": ("openwrt", "package/network/config/netifd", ("netifd",), "openwrt/netifd"),
    "umdns": ("openwrt", "package/network/services/umdns", ("umdns",), "openwrt/mdnsd"),
    "hostapd": ("openwrt", "package/network/services/hostapd", ("wpad*", "hostapd*", "wpa-supplicant*"), None),
    "mbedtls": ("openwrt", "package/libs/mbedtls", ("libmbedtls*",), None),
    "wolfssl": ("openwrt", "package/libs/wolfssl", ("libwolfssl*",), None),
    "openssl": ("openwrt", "package/libs/openssl", ("libopenssl*", "openssl-util"), None),
    "cgi-io": ("packages", "net/cgi-io", ("cgi-io",), "openwrt/cgi-io"),
    "curl": ("packages", "net/curl", ("curl", "libcurl*"), None),
    "nghttp2": ("packages", "libs/nghttp2", ("libnghttp2*", "nghttp2*"), None),
    "expat": ("packages", "libs/expat", ("libexpat*",), None),
    "c-ares": ("packages", "libs/c-ares", ("libcares*",), None),
    "bind": ("packages", "net/bind", ("bind-*",), None),
    "tailscale": ("packages", "net/tailscale", ("tailscale",), None),
    "golang": ("packages", "lang/golang/golang1.26", (), None),
    "xray-core": ("passwall_packages", "xray-core", ("xray-core",), None),
    "sing-box": ("passwall_packages", "sing-box", ("sing-box",), None),
    "passwall": ("passwall", "luci-app-passwall", ("luci-app-passwall",), None),
    "openclash": ("openclash", "luci-app-openclash", ("luci-app-openclash",), None),
}
SECURITY_FIX = re.compile(
    r"CVE-\d{4}-\d{4,}|GHSA-[a-z0-9-]+|security (?:advisory|fix|release)|"
    r"use.after.free|buffer (?:over|under)(?:flow|read)|out.of.bounds (?:read|write)|"
    r"request smuggling|memory.exhaustion|\bvulnerabilit(?:y|ies)\b|漏洞修复", re.I)
GHSA = re.compile(r"GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}", re.I)
UHTTPD_FIXES = {
    "GHSA-83vv-qrc6-h3hx": "42e30caa704e4e6e28fc7412e0006959eb247c44",
    "GHSA-2mpg-6wp5-435p": "e76736e5676fb25536b27d45865eb4153c68aca3",
    "GHSA-vhx4-3p5q-m59q": "f6c2fcfa539de49ddf6de8f805110c8804602e98",
    "GHSA-wvgh-cm54-q6f6": "3c48b9e17d2086f9756b97e5825c2ebdf3ed233a",
    "GHSA-c2wg-hcff-hqrm": "d6205aad61763b5e5d86441164b3aeed83294e7f",
}
MOUNTS_ADVISORY = "GHSA-v5f9-62c7-cw29"
# Each inner group requires any feature; all groups must be satisfied.
TAILSCALE_CONDITIONS = {
    "TS-2026-011": (("tailscale_4via6",),),
    "TS-2026-010": (("tailscale_ssh",), ("tailscale_ssh_accept_env",)),
    "TS-2026-009": (("tailscale_ssh",), ("tailscale_ssh_nonroot_policy",)),
    "TS-2026-008": (("tailscale_serve", "tailscale_funnel"),),
    "TS-2026-007": (("tailscale_services",),),
    "TS-2026-006": (("tailscale_ssh",), ("tailscale_ssh_nonroot_policy",)),
    "TS-2026-005": (("tailscale_serve",), ("tailscale_nonroot_operator",), ("tailscale_privileged_sockets",)),
    "TS-2026-004": (("tailscale_ssh",), ("tailscale_shared_socket_permissions",)),
}


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("Security policy must be a JSON object")
    features = {"luci_delegated_users"} | {key for groups in TAILSCALE_CONDITIONS.values() for group in groups for key in group}
    values = policy.get("features", {})
    if policy.get("schema") != 1 or not isinstance(policy.get("basis"), str) or not policy["basis"].strip():
        raise ValueError("Security policy requires a schema and an explicit usage basis")
    if not isinstance(values, dict) or any(key not in features or value is not None and type(value) is not bool
                                          for key, value in values.items()):
        raise ValueError("Security policy features must be known booleans or null")
    return policy


def applicability(identifier, policy):
    values = (policy or {}).get("features", {})
    groups = (("luci_delegated_users",),) if identifier == MOUNTS_ADVISORY else TAILSCALE_CONDITIONS.get(identifier)
    if not groups:
        return None
    known = [True if any(values.get(key) is True for key in group) else
             False if all(values.get(key) is False for key in group) else None for group in groups]
    return False if False in known else True if all(value is True for value in known) else None


def packages_for(name, inventory):
    if name == "golang":
        current = inventory.get("build_tools", {}).get("golang")
        return {"golang/host": current} if current else {}
    selectors = COMPONENTS[name][2] if name in COMPONENTS else (name,)
    return {package: version for package, version in inventory["packages"].items()
            if any(fnmatchcase(package, selector) for selector in selectors)}


def content(github, repo, path, ref):
    entry = github.get(f"repos/{repo}/contents/{path}?" + urlencode({"ref": ref}))
    if not isinstance(entry, dict) or entry.get("encoding") != "base64":
        raise ValueError("Source file content is unavailable")
    return base64.b64decode(entry["content"]).decode()


def version(value):
    match = re.fullmatch(r"v?(\d+(?:\.\d+)+)(?:-r\d+)?", value or "")
    return tuple(map(int, match[1].split("."))) if match else None


def fixed_version(advisory, installed):
    """Only resolve explicit stable patched versions; leave other ranges open."""
    current = version(installed)
    items = advisory.get("vulnerabilities") or []
    if not current or not items:
        return False
    for item in items:
        for value in (item.get("patched_versions") or "").split(","):
            patched = re.fullmatch(r"\s*(?:>=\s*)?(v?\d+(?:\.\d+)+)\s*", value)
            if not patched or current < version(patched[1]):
                return False
    return True


def scope_exclusion(sha, inventory):
    # Reviewed scope of these immutable commits. Do not generalize their
    # exclusions to future fixes for the same component.
    if sha == "95e72dc9180d33334e026ae1a96a07e1b12237c2" and inventory["flags"].get("CONFIG_DRIVER_11BE_SUPPORT") == "n":
        return "This MLO/EHT fix requires 802.11be support, disabled in this build"
    if sha == "1e02e8d8f1feb74151ef1c06c16432eea774f7b8" and "bind-server" not in inventory["packages"]:
        return "Only bind-dig/bind-libs are installed; named resolver issues require separate applicability review"
    return None


def assess(github, lock, inventory, report, policy=None):
    actions, errors = [], []
    sources = {source["name"]: source for source in report["sources"]}
    advisory_fixes = {}
    fixed_advisories, component_pins = {}, {}
    included_commits = {}
    updates = inventory.get("verified_package_updates", {})
    if updates != lock.get("package_updates", {}):
        raise ValueError("Package update provenance does not match the source lock")
    for name, update in updates.items():
        if name not in COMPONENTS or (update["source"], update["path"]) != COMPONENTS[name][:2]:
            raise ValueError("Package update does not match the monitored component")
    if inventory.get("verified_hardening", []) != lock.get("hardening", []):
        raise ValueError("Firmware hardening provenance does not match the source lock")
    if policy is not None:
        report["usage_policy"] = validate_policy(policy)

    def add(name, key, title, url, message, kind, identifiers=None):
        identifiers = sorted(set(GHSA.findall(message) if identifiers is None else identifiers))
        sha = key.rsplit(":", 1)[-1]
        if kind == "component_security_fix":
            for existing in actions:
                if existing["component"] == name and any(sha.startswith(prefix) for prefix in included_commits.get(existing["key"], [])):
                    existing["advisories"] = sorted(set(existing["advisories"] + identifiers))
                    existing.setdefault("upstream_fixes", []).append(url)
                    advisory_fixes.update({identifier: url for identifier in identifiers})
                    return
        elif kind == "feed_security_update":
            included_commits[key] = re.findall(r"\b[0-9a-f]{12,40}\b", message)
        action = {"key": key, "component": name, "packages": packages_for(name, inventory),
                  "title": title, "url": url, "kind": kind, "advisories": identifiers,
                  "action": "将安全修复纳入锁定源码，重新构建并刷入固件"}
        actions.append(action)
        for identifier in action["advisories"]:
            advisory_fixes[identifier] = url

    for signal in report["security_signals"]:
        signal["assessment"] = "needs_review"
        if not signal["key"].startswith("commit:"):
            continue
        _, repo, sha = signal["key"].split(":", 2)
        name = signal["title"].split(":", 1)[0].strip().lower()
        if name.startswith("luci-"):
            selected = packages_for(name, inventory)
            paths = (f"applications/{name}/", f"modules/{name}/", f"protocols/{name}/", f"libs/{name}/")
            feed = "luci"
            expected_repo = sources.get("luci", {}).get("repository")
        elif name in COMPONENTS:
            selected = packages_for(name, inventory)
            feed, path, _, _ = COMPONENTS[name]
            paths, expected_repo = (path + "/",), sources.get(feed, {}).get("repository")
        else:
            continue
        if not selected:
            # Go's standard library is linked into installed applications even
            # when the compiler is absent from the target package manifest.
            signal["assessment"] = "needs_review" if name == "golang" else "not_installed"
            continue
        update = updates.get(name)
        if not update and any(path.startswith(paths) for path in inventory.get("source_overrides", {}).get(feed, [])):
            signal.update(assessment="needs_review", reason="Custom source changes require backport verification")
            continue
        excluded = scope_exclusion(sha, inventory)
        if excluded:
            signal.update(assessment="scope_excluded", reason=excluded)
            continue
        if repo != expected_repo:
            continue
        try:
            change = github.get(f"repos/{repo}/commits/{sha}")
            message = change["commit"]["message"]
            if update:
                status = "identical" if sha == update["commit"] else github.get(
                    f"repos/{repo}/compare/{sha}...{update['commit']}")["status"]
                if status in ("ahead", "identical"):
                    signal["assessment"] = "fixed_in_inventory"
                    fixed_advisories.update({key: signal["url"] for key in GHSA.findall(message)})
                    continue
                if status != "behind":
                    signal.update(assessment="needs_review", reason="Package update and feed fix have diverged histories")
                    continue
            if not SECURITY_FIX.search(message) or not any(
                    item["filename"].startswith(paths) for item in change["files"]):
                continue
            identifiers = GHSA.findall(message)
            if identifiers and all(applicability(key, policy) is False for key in identifiers):
                signal.update(assessment="scope_excluded", reason="Excluded by the declared usage policy")
                continue
            signal["assessment"] = "fix_pending"
            add(name, signal["key"], signal["title"], signal["url"], message, "feed_security_update")
        except (RuntimeError, ValueError, KeyError, TypeError) as error:
            errors.append(f"Assess {name}: {error}")

    # Feed update messages can omit advisories. Follow the exact packaged
    # component commit as well, to catch fixes not yet backported to the feed.
    for name, (feed, path, _, upstream) in COMPONENTS.items():
        if not upstream or not packages_for(name, inventory) or feed not in sources:
            continue
        source = sources[feed]
        update = updates.get(name)
        recipe_ref = update["commit"] if update else source["commit"]
        try:
            if not update and any(filename.startswith(path + "/") for filename in inventory.get("source_overrides", {}).get(feed, [])):
                report.setdefault("assessment_notes", []).append(f"{name}: custom source changes require backport verification")
                continue
            makefile = content(github, source["repository"], path + "/Makefile", recipe_ref)
            pinned = re.search(r"^PKG_SOURCE_VERSION\s*:?=\s*([0-9a-f]{40})\s*$", makefile, re.M)
            if not pinned:
                raise ValueError("Packaged component commit cannot be resolved")
            entries = github.get(f"repos/{source['repository']}/contents/{path}?" + urlencode({"ref": recipe_ref}))
            if any(entry["name"] == "patches" for entry in entries):
                report.setdefault("assessment_notes", []).append(
                    f"{name}: local patch directory requires review before claiming an upstream fix is missing")
                continue
            component_pins[name] = pinned[1]
            branch = "openwrt-" + ".".join(lock["openwrt"]["release"].split(".")[:2])
            branches = github.pages(f"repos/{upstream}/branches")
            matching = next((item["commit"]["sha"] for item in branches if item["name"] == branch), None)
            head = matching or github.get(f"repos/{upstream}/commits/HEAD")["sha"]
            comparison = f"repos/{upstream}/compare/{pinned[1]}...{head}"
            compared = github.get(comparison + "?per_page=100&page=1")
            if compared["status"] not in ("ahead", "identical"):
                raise ValueError("Component source is not an ancestor of its upstream")
            commits = compared["commits"] if compared["total_commits"] <= len(compared["commits"]) else github.pages(comparison, "commits")
            if len(commits) != compared["total_commits"]:
                raise ValueError("Component comparison is incomplete")
            for change in commits:
                if update and change["sha"] in {patch["commit"] for patch in update.get("patches", [])}:
                    continue
                message = change["commit"]["message"]
                title = message.splitlines()[0]
                if title.startswith(("Merge ", "Revert ")) or not SECURITY_FIX.search(message):
                    continue
                if name == "uhttpd" and title.startswith("ubus:") and "uhttpd-mod-ubus" not in inventory["packages"]:
                    continue
                add(name, f"commit:{upstream}:{change['sha']}", title, change["html_url"], message, "component_security_fix")
        except (RuntimeError, ValueError, KeyError, TypeError) as error:
            errors.append(f"Assess {name} upstream fixes: {error}")

    for advisory in report["advisories"]:
        identifier = advisory["id"]
        if identifier in inventory.get("verified_hardening", []):
            advisory.update(status="fixed_in_inventory", reason="Firmware ACL hardening was verified in this build")
            continue
        if identifier in fixed_advisories:
            advisory.update(status="fixed_in_inventory", fix_url=fixed_advisories[identifier])
            continue
        if advisory["repository"] == "openwrt/uhttpd" and identifier in UHTTPD_FIXES and "uhttpd" in component_pins:
            fix = UHTTPD_FIXES[identifier]
            try:
                comparison = github.get(f"repos/openwrt/uhttpd/compare/{fix}...{component_pins['uhttpd']}")
                if comparison["status"] in ("ahead", "identical"):
                    advisory.update(status="fixed_in_inventory", fix_url=f"https://github.com/openwrt/uhttpd/commit/{fix}")
                    continue
            except (RuntimeError, ValueError, KeyError, TypeError) as error:
                errors.append(f"Assess {identifier}: {error}")
        if advisory["id"] in advisory_fixes:
            advisory.update(status="fix_pending", fix_url=advisory_fixes[advisory["id"]])
            continue
        title = advisory["title"]
        package_names = set(re.findall(r"\bluci-(?:app|proto|lib)-[a-z0-9-]+", title))
        if identifier == "GHSA-8qcq-jgrj-gvmj":
            package_names = {"luci-app-bmx7"}
        elif identifier == MOUNTS_ADVISORY:
            package_names = {"luci-mod-system"}
        if "luci-lib-px5g" in title:
            package_names = {"luci-lib-px5g"}
        if package_names and not any(name in inventory["packages"] for name in package_names):
            advisory["status"] = "not_installed"
            continue
        if identifier == "GHSA-vvj6-7362-pjrw":
            current = inventory["packages"].get("luci-mod-network")
            if not current:
                advisory["status"] = "not_installed"
                continue
            date_version = re.fullmatch(r"(\d+\.\d+\.\d+)~[0-9a-f]+(?:-r\d+)?", current)
            custom = any(path.startswith("modules/luci-mod-network/") for path in inventory.get("source_overrides", {}).get("luci", []))
            if date_version and version(date_version[1]) >= (26, 72, 65753) and not custom:
                advisory.update(status="fixed_in_inventory", reason="LuCI build is at or after the published fixed version")
                continue
        if identifier == MOUNTS_ADVISORY:
            advisory["condition"] = "Requires a delegated LuCI/rpcd account with mount-configuration write access"
            if applicability(identifier, policy) is False:
                advisory.update(status="configuration_excluded", reason="The owner declares no delegated LuCI administrators")
            elif applicability(identifier, policy) is True:
                add("luci-mod-system", "advisory:" + identifier, title, advisory["url"], identifier, "acl_security_fix")
                advisory["status"] = "fix_pending"
            else:
                advisory["status"] = "configuration_review"
            continue
        if advisory["repository"] == "openwrt/mdnsd" and "umdns" not in inventory["packages"]:
            advisory["status"] = "not_installed"
            continue
        if advisory["repository"] == "tailscale/tailscale" and re.search(r"\b(?:Windows|FreeBSD)\b", title):
            advisory["status"] = "other_platform"
            continue
        name = {"XTLS/Xray-core": "xray-core", "SagerNet/sing-box": "sing-box",
                "tailscale/tailscale": "tailscale", "MetaCubeX/mihomo": "mihomo"}.get(advisory["repository"])
        if name:
            installed = inventory["packages"].get(name)
            if not installed:
                advisory["status"] = "not_installed"
            elif fixed_version(advisory, installed):
                advisory["status"] = "fixed_in_inventory"
            elif advisory.get("vendor") == "tailscale":
                if applicability(identifier, policy) is False:
                    advisory.update(status="configuration_excluded", reason="The owner-declared usage lacks a required condition")
                elif version(installed) and applicability(identifier, policy) is True and any(
                        version(item.get("patched_versions")) for item in advisory.get("vulnerabilities", [])):
                    add(name, "advisory:" + identifier, title, advisory["url"], "", "vendor_security_fix", [identifier])
                    advisory["status"] = "fix_pending"
                else:
                    advisory.update(status="configuration_review", reason="Installed version and vendor scope require runtime configuration review")

    report["inventory"] = {key: inventory[key] for key in ("recipe_digest", "recipe_commit", "workflow_run", "packages")}
    release_packages = {"XTLS/Xray-core": "xray-core", "SagerNet/sing-box": "sing-box", "tailscale/tailscale": "tailscale"}
    releases = []
    for release in report.get("releases", []):
        name = release_packages.get(release["repository"])
        if name:
            current = inventory["packages"].get(name)
            if not current:
                continue
            release["current"] = current
            if version(current) and version(release["latest"]) and version(release["latest"]) <= version(current):
                continue
        releases.append(release)
    report["releases"] = releases
    report["actions"] = actions
    severity = {item["id"]: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item.get("severity"), 4)
                for item in report["advisories"]}
    actions.sort(key=lambda item: (min((severity.get(key, 4) for key in item["advisories"]), default=4), item["component"], item["key"]))
    report["errors"].extend(errors)
    return actions

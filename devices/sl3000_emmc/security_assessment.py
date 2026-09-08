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


def packages_for(name, inventory):
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


def assess(github, lock, inventory, report):
    actions, errors = [], []
    sources = {source["name"]: source for source in report["sources"]}
    advisory_fixes = {}
    included_commits = {}

    def add(name, key, title, url, message, kind):
        identifiers = sorted(set(GHSA.findall(message)))
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
            signal["assessment"] = "not_installed"
            continue
        if any(path.startswith(paths) for path in inventory.get("source_overrides", {}).get(feed, [])):
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
            if not SECURITY_FIX.search(message) or not any(
                    item["filename"].startswith(paths) for item in change["files"]):
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
        try:
            if any(filename.startswith(path + "/") for filename in inventory.get("source_overrides", {}).get(feed, [])):
                report.setdefault("assessment_notes", []).append(f"{name}: custom source changes require backport verification")
                continue
            makefile = content(github, source["repository"], path + "/Makefile", source["commit"])
            pinned = re.search(r"^PKG_SOURCE_VERSION\s*:?=\s*([0-9a-f]{40})\s*$", makefile, re.M)
            if not pinned:
                raise ValueError("Packaged component commit cannot be resolved")
            entries = github.get(f"repos/{source['repository']}/contents/{path}?" + urlencode({"ref": source["commit"]}))
            if any(entry["name"] == "patches" for entry in entries):
                report.setdefault("assessment_notes", []).append(
                    f"{name}: local patch directory requires review before claiming an upstream fix is missing")
                continue
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
        if advisory["id"] in advisory_fixes:
            advisory.update(status="fix_pending", fix_url=advisory_fixes[advisory["id"]])
            continue
        title = advisory["title"]
        package_names = set(re.findall(r"\bluci-(?:app|proto|lib)-[a-z0-9-]+", title))
        if "luci-lib-px5g" in title:
            package_names = {"luci-lib-px5g"}
        if package_names and not any(name in inventory["packages"] for name in package_names):
            advisory["status"] = "not_installed"
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

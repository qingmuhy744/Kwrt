import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import security_assessment as assessment
import security_inventory as inventory


def baseline():
    return {"schema": 1, "recipe_digest": "digest", "recipe_commit": "a" * 40, "workflow_run": "12",
            "packages": {"kernel": "6.12-r1", "base-files": "1", "luci-base": "1", "uhttpd": "2026.06.16~7b1bec45-r1"},
            "flags": {"CONFIG_DRIVER_11BE_SUPPORT": "n"}}


def report():
    return {"sources": [{"name": "openwrt", "repository": "openwrt/openwrt", "commit": "a" * 40}],
            "security_signals": [], "advisories": [], "errors": []}


LOCK = {"openwrt": {"release": "25.12.5"}}


class FakeGitHub:
    def __init__(self, patches=False, failure=False, message="Fixes: GHSA-abcd-efgh-ijkl"):
        self.patches, self.failure, self.message = patches, failure, message

    def get(self, path):
        if self.failure:
            raise RuntimeError("upstream unavailable")
        if "Makefile?" in path:
            return {"encoding": "base64", "content": base64.b64encode(
                ("PKG_SOURCE_VERSION:=" + "b" * 40 + "\n").encode()).decode()}
        if "/contents/" in path:
            return [{"name": "patches"}] if self.patches else [{"name": "Makefile"}]
        if "/commits/HEAD" in path:
            return {"sha": "c" * 40}
        if "/compare/" in path:
            return {"status": "ahead", "total_commits": 1, "commits": [
                {"sha": "d" * 40, "commit": {"message": self.message}, "html_url": "https://example.test/fix"}]}
        return {"commit": {"message": self.message}, "files": [{"filename": "package/libs/mbedtls/Makefile"}]}

    def pages(self, path):
        return []


class InventoryTests(unittest.TestCase):
    def test_manifest_must_be_complete_and_unique(self):
        packages = baseline()["packages"]
        text = "\n".join(f"{name} - {value}" for name, value in packages.items())
        self.assertEqual(inventory.parse_manifest(text), packages)
        for invalid in (text + "\nuhttpd - 2", "uhttpd - 1", "uhttpd 1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                inventory.parse_manifest(invalid)

    def test_build_inputs_invalidate_inventory_but_monitor_changes_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".config").write_text("CONFIG_PACKAGE_uhttpd=y\n")
            initial = inventory.recipe_digest(root)
            (root / "security_monitor.py").write_text("new notification formatting")
            self.assertEqual(inventory.recipe_digest(root), initial)
            (root / ".config").write_text("CONFIG_PACKAGE_uhttpd=n\n")
            self.assertNotEqual(inventory.recipe_digest(root), initial)

    def test_checked_in_inventory_is_valid_for_its_recorded_recipe_only(self):
        data = json.loads((ROOT / "security-baseline.json").read_text())
        self.assertTrue(inventory.validate(data, data["recipe_digest"], data["lock"]))
        self.assertFalse(inventory.validate(data, "different recipe", data["lock"]))
        self.assertFalse(inventory.validate(data, data["recipe_digest"], {"new": "lock"}))

    def test_update_provenance_also_requires_the_fixed_binary_version(self):
        data = baseline()
        lock = {"package_updates": {"uhttpd": {"packages": {"uhttpd": "2026.08.03~60f64bec-r2"}}}}
        data.update(lock=lock, verified_package_updates=lock["package_updates"])
        with self.assertRaisesRegex(ValueError, "Security update missing"):
            inventory.validate(data, "digest", lock)
        data["packages"]["uhttpd"] = "2026.08.03~60f64bec-r2"
        self.assertTrue(inventory.validate(data, "digest", lock))
        data.pop("verified_package_updates")
        self.assertFalse(inventory.validate(data, "digest", lock))

    def test_missing_matching_build_is_an_error(self):
        class NoBuilds:
            def get(self, path):
                return {"workflow_runs": []} if "/actions/" in path else {"default_branch": "main"}
        with tempfile.TemporaryDirectory() as directory, patch.object(inventory, "HERE", Path(directory)), patch.object(
                inventory, "recipe_digest", return_value="new digest"):
            with self.assertRaisesRegex(ValueError, "No successful build inventory"):
                inventory.load(NoBuilds(), "example/repo", {}, {})

    def test_new_build_inventory_is_downloaded_and_checked_against_build_provenance(self):
        data = baseline()
        data["lock"] = {}

        class Builds:
            def get(self, path):
                if "/artifacts?" in path:
                    return {"artifacts": [{"name": "sl3000-security-inventory-12", "expired": False}]}
                if "/actions/" in path:
                    return {"workflow_runs": [{"id": 12, "head_sha": "a" * 40}]}
                return {"default_branch": "main"}

        def download(args, **kwargs):
            (Path(args[-1]) / "security-inventory.json").write_text(json.dumps(data))
            from subprocess import CompletedProcess
            return CompletedProcess(args, 0)

        with tempfile.TemporaryDirectory() as directory, patch.object(inventory, "HERE", Path(directory)), patch.object(
                inventory, "recipe_digest", return_value="digest"), patch.object(inventory.subprocess, "run", side_effect=download):
            self.assertEqual(inventory.load(Builds(), "example/repo", {}, {}), data)
            data["recipe_commit"] = "b" * 40
            with self.assertRaisesRegex(ValueError, "provenance"):
                inventory.load(Builds(), "example/repo", {}, {})

    def test_collection_records_custom_sources_without_configuration_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = {"feeds": {"luci": {}}}
            (root / "sources.lock.json").write_text(json.dumps(lock))
            (root / "openwrt-mediatek-filogic-sl_3000-emmc.manifest").write_text(
                "\n".join(f"{name} - {value}" for name, value in baseline()["packages"].items()))
            (root / "build-info.json").write_text(json.dumps({
                "recipe_commit": "a" * 40, "workflow_run": "12", "supported_devices": ["sl,3000-emmc"]}))
            (root / "openwrt.config").write_text(
                'CONFIG_MBEDTLS_AES_C=y\n# CONFIG_DRIVER_11BE_SUPPORT is not set\nCONFIG_MBEDTLS_PRIVATE="private-value"\n')
            with patch.object(inventory.subprocess, "check_output", side_effect=[
                    "package/network/services/uhttpd/Makefile\n", "package/network/services/uhttpd/patches/fix.patch\n", "", ""]):
                data = inventory.collect(root, root=root, openwrt=root / "openwrt")
            self.assertEqual(len(data["source_overrides"]["openwrt"]), 2)
            self.assertEqual(data["flags"]["CONFIG_DRIVER_11BE_SUPPORT"], "n")
            self.assertNotIn("private-value", json.dumps(data))


class AssessmentTests(unittest.TestCase):
    def test_root_only_policy_excludes_only_the_reviewed_delegation_issue(self):
        for delegated, expected, count in ((False, "configuration_excluded", 0),
                                            (None, "configuration_review", 0), (True, "fix_pending", 1)):
            with self.subTest(delegated=delegated):
                data, result = baseline(), report()
                data["packages"] = {"luci-mod-system": "26.180.75667~128a781"}
                result["advisories"] = [{"id": assessment.MOUNTS_ADVISORY, "repository": "openwrt/luci", "title": "Mount ACL",
                                         "url": "https://example.test/mounts"}]
                policy = {"schema": 1, "basis": "Owner declaration", "features": {"luci_delegated_users": delegated}}
                self.assertEqual(len(assessment.assess(FakeGitHub(), LOCK, data, result, policy)), count)
                self.assertEqual(result["advisories"][0]["status"], expected)
                self.assertIsNone(assessment.applicability("GHSA-unreviewed", policy))

    def test_hardening_fix_needs_verified_build_provenance(self):
        lock = {**LOCK, "hardening": [assessment.MOUNTS_ADVISORY]}
        data, result = baseline(), report()
        with self.assertRaisesRegex(ValueError, "hardening provenance"):
            assessment.assess(FakeGitHub(), lock, data, result)
        data["packages"] = {"luci-mod-system": "26.180.75667~128a781"}
        data["verified_hardening"] = lock["hardening"]
        result["advisories"] = [{"id": assessment.MOUNTS_ADVISORY, "repository": "openwrt/luci", "title": "Mount ACL"}]
        self.assertEqual(assessment.assess(FakeGitHub(), lock, data, result), [])
        self.assertEqual(result["advisories"][0]["status"], "fixed_in_inventory")

    def test_included_uhttpd_commits_are_linked_back_to_advisories(self):
        result = report()
        result["advisories"] = [{"id": key, "title": "uhttpd issue", "repository": "openwrt/uhttpd"}
                                for key in assessment.UHTTPD_FIXES]
        assessment.assess(FakeGitHub(message="ordinary update"), LOCK, baseline(), result)
        self.assertTrue(all(item["status"] == "fixed_in_inventory" for item in result["advisories"]))

    def test_nonancestor_uhttpd_fix_is_not_claimed_as_included(self):
        class Behind(FakeGitHub):
            def get(self, path):
                if any(f"/compare/{sha}..." in path for sha in assessment.UHTTPD_FIXES.values()):
                    return {"status": "behind"}
                return super().get(path)
        result = report()
        result["advisories"] = [{"id": next(iter(assessment.UHTTPD_FIXES)), "title": "uhttpd", "repository": "openwrt/uhttpd",
                                 "status": "needs_review"}]
        assessment.assess(Behind(message="ordinary update"), LOCK, baseline(), result)
        self.assertEqual(result["advisories"][0]["status"], "needs_review")

    def test_bmx7_absence_and_fixed_wifi_scan_are_classified(self):
        data, result = baseline(), report()
        data["packages"] = {"luci-mod-network": "26.180.75667~128a781"}
        result["advisories"] = [
            {"id": "GHSA-8qcq-jgrj-gvmj", "title": "Unchecked query traversal", "repository": "openwrt/luci"},
            {"id": "GHSA-vvj6-7362-pjrw", "title": "WiFi scan XSS", "repository": "openwrt/luci"}]
        assessment.assess(FakeGitHub(), LOCK, data, result)
        self.assertEqual([item["status"] for item in result["advisories"]], ["not_installed", "fixed_in_inventory"])

    def test_vendor_advisory_requires_an_installed_unfixed_version_and_known_usage(self):
        for current, features, status, count in (
                (None, {}, "not_installed", 0), ("1.102.3-r1", {}, "fixed_in_inventory", 0),
                ("1.98.3-r1", {}, "configuration_review", 0),
                ("custom-build", {"tailscale_serve": True}, "configuration_review", 0),
                ("1.98.3-r1", {"tailscale_serve": False, "tailscale_funnel": False}, "configuration_excluded", 0),
                ("1.98.3-r1", {"tailscale_serve": True}, "fix_pending", 1)):
            with self.subTest(current=current, features=features):
                data, result = baseline(), report()
                data["packages"] = {"tailscale": current} if current else {}
                result["advisories"] = [{"id": "TS-2026-008", "repository": "tailscale/tailscale", "vendor": "tailscale",
                                         "title": "Serve availability", "url": "https://tailscale.com/security-bulletins/#ts-2026-008",
                                         "vulnerabilities": [{"patched_versions": "1.98.9"}]}]
                policy = {"schema": 1, "basis": "Test usage", "features": features}
                self.assertEqual(len(assessment.assess(FakeGitHub(), LOCK, data, result, policy)), count)
                self.assertEqual(result["advisories"][0]["status"], status)

    def test_absent_target_go_package_does_not_exclude_embedded_standard_library(self):
        data, result = baseline(), report()
        data["packages"] = {"tailscale": "1.98.3-r1"}
        result["security_signals"] = [{"key": "commit:openwrt/packages:" + "a" * 40, "title": "golang: security update",
                                        "url": "https://example.test/go"}]
        assessment.assess(FakeGitHub(), LOCK, data, result)
        self.assertEqual(result["security_signals"][0]["assessment"], "needs_review")

    def test_string_false_cannot_silently_disable_alerts(self):
        with self.assertRaisesRegex(ValueError, "booleans"):
            assessment.validate_policy({"schema": 1, "basis": "test", "features": {"luci_delegated_users": "false"}})

    def test_verified_package_update_uses_new_recipe_and_skips_only_declared_backport(self):
        update = {"source": "openwrt", "path": "package/network/services/uhttpd", "commit": "e" * 40,
                  "patches": [{"commit": "d" * 40}]}
        lock = {**LOCK, "package_updates": {"uhttpd": update}}
        data = baseline()
        data["verified_package_updates"] = lock["package_updates"]
        data["source_overrides"] = {"openwrt": [update["path"] + "/Makefile", update["path"] + "/patches/fix.patch"]}

        class UpdatedGitHub(FakeGitHub):
            def get(self, path):
                if "/contents/" in path:
                    assert "ref=" + "e" * 40 in path
                result = super().get(path)
                if "/compare/" in path:
                    result["commits"].append({"sha": "f" * 40, "commit": {"message": "Fixes CVE-2026-99999"},
                                              "html_url": "https://example.test/new-fix"})
                    result["total_commits"] = 2
                return result

        result = report()
        actions = assessment.assess(UpdatedGitHub(), lock, data, result)
        self.assertEqual(result["errors"], [])
        self.assertEqual([action["key"] for action in actions], ["commit:openwrt/uhttpd:" + "f" * 40])

    def test_feed_security_update_already_imported_is_not_reported_again(self):
        update = {"source": "openwrt", "path": "package/libs/mbedtls", "commit": "e" * 40}
        lock = {**LOCK, "package_updates": {"mbedtls": update}}
        data, result = baseline(), report()
        data["packages"] = {"libmbedtls21": "3.6.7-r1"}
        data["verified_package_updates"] = lock["package_updates"]
        data["source_overrides"] = {"openwrt": [update["path"] + "/Makefile"]}
        result["security_signals"] = [{"key": "commit:openwrt/openwrt:" + "e" * 40,
                                        "title": "mbedtls: security update", "url": "https://example.test/fix"}]
        self.assertEqual(assessment.assess(FakeGitHub(), lock, data, result), [])
        self.assertEqual(result["security_signals"][0]["assessment"], "fixed_in_inventory")

    def test_missing_package_update_provenance_cannot_hide_a_fix(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            assessment.assess(FakeGitHub(), {**LOCK, "package_updates": {"uhttpd": {}}}, baseline(), report())

    def test_missing_upstream_fix_for_an_installed_component_is_actionable(self):
        data, result = baseline(), report()
        result["advisories"] = [{"id": "GHSA-abcd-efgh-ijkl", "title": "uhttpd issue", "repository": "openwrt/uhttpd"}]
        actions = assessment.assess(FakeGitHub(), LOCK, data, result)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["component"], "uhttpd")
        self.assertEqual(result["advisories"][0]["status"], "fix_pending")

    def test_local_backports_require_review_not_a_false_missing_fix_alert(self):
        result = report()
        self.assertEqual(assessment.assess(FakeGitHub(patches=True), LOCK, baseline(), result), [])
        self.assertIn("local patch", result["assessment_notes"][0])

    def test_custom_build_patches_prevent_false_missing_fix_claims(self):
        data, result = baseline(), report()
        data["source_overrides"] = {"openwrt": ["package/network/services/uhttpd/patches/local-fix.patch"]}
        self.assertEqual(assessment.assess(FakeGitHub(), LOCK, data, result), [])
        self.assertIn("custom source", result["assessment_notes"][0])

    def test_normal_commits_and_reverts_are_not_security_actions(self):
        for message in ("Enable WPA3 compatibility for better security", "Revert security fix for CVE-2026-12345"):
            with self.subTest(message=message):
                self.assertEqual(assessment.assess(FakeGitHub(message=message), LOCK, baseline(), report()), [])

    def test_unavailable_upstream_is_not_a_clean_scan(self):
        result = report()
        self.assertEqual(assessment.assess(FakeGitHub(failure=True), LOCK, baseline(), result), [])
        self.assertEqual(len(result["errors"]), 1)

    def test_tls_variants_and_abi_names_match_only_installed_packages(self):
        data = baseline()
        data["packages"]["libmbedtls21"] = "3.6.6-r2"
        self.assertEqual(assessment.packages_for("mbedtls", data), {"libmbedtls21": "3.6.6-r2"})
        self.assertEqual(assessment.packages_for("wolfssl", data), {})

    def test_wifi7_fix_is_excluded_when_not_compiled(self):
        data = baseline()
        self.assertIsNotNone(assessment.scope_exclusion("95e72dc9180d33334e026ae1a96a07e1b12237c2", data))
        data["flags"]["CONFIG_DRIVER_11BE_SUPPORT"] = "y"
        self.assertIsNone(assessment.scope_exclusion("95e72dc9180d33334e026ae1a96a07e1b12237c2", data))
        self.assertIsNone(assessment.scope_exclusion("some-future-hostapd-fix", baseline()))

    def test_uninstalled_apps_fixed_proxy_versions_and_foreign_platforms_are_excluded(self):
        result, data = report(), baseline()
        data["packages"].update({"sing-box": "1.14.0-r1", "xray-core": "26.7.28-r1"})
        result["advisories"] = [
            {"id": "one", "title": "luci-app-openvpn root execution", "repository": "openwrt/luci"},
            {"id": "two", "title": "Tailscale Windows RCE", "repository": "tailscale/tailscale"},
            {"id": "three", "title": "SOCKS auth issue", "repository": "SagerNet/sing-box",
             "vulnerabilities": [{"patched_versions": "1.4.5"}]},
            {"id": "four", "title": "Certificate pinning", "repository": "XTLS/Xray-core",
             "vulnerabilities": [{"patched_versions": ">= v26.7.11"}]},
        ]
        assessment.assess(FakeGitHub(message="ordinary update"), LOCK, data, result)
        self.assertEqual([item["status"] for item in result["advisories"]],
                         ["not_installed", "other_platform", "fixed_in_inventory", "fixed_in_inventory"])

    def test_feed_fix_requires_correct_repository_path_and_security_evidence(self):
        data = baseline()
        data["packages"].pop("uhttpd")
        data["packages"]["libmbedtls21"] = "3.6.6-r2"
        for repo, message, count in (("openwrt/openwrt", "Fixes CVE-2026-12345", 1),
                                     ("other/repo", "Fixes CVE-2026-12345", 0),
                                     ("openwrt/openwrt", "new feature", 0)):
            with self.subTest(repo=repo, message=message):
                result = report()
                result["security_signals"] = [{"key": f"commit:{repo}:abcd", "title": "mbedtls: update",
                                                "url": "https://example.test/fix"}]
                self.assertEqual(len(assessment.assess(FakeGitHub(message=message), LOCK, data, result)), count)

    def test_component_release_branch_is_preferred_over_master(self):
        class ReleaseGitHub(FakeGitHub):
            def pages(self, path):
                return [{"name": "openwrt-25.12", "commit": {"sha": "e" * 40}}]

            def get(self, path):
                if "/commits/HEAD" in path:
                    raise AssertionError("Do not compare the release package against master")
                if "/compare/" in path:
                    assert "..." + "e" * 40 in path
                return super().get(path)

        result = report()
        self.assertEqual(len(assessment.assess(ReleaseGitHub(), LOCK, baseline(), result)), 1)
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()

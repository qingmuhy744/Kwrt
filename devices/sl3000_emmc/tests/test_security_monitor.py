import io
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import security_monitor as monitor


NOW = datetime(2026, 9, 8, 2, tzinfo=timezone.utc)


def commit(days, sha="a" * 40):
    return {"sha": sha, "commit": {"committer": {"date": monitor.iso(NOW - timedelta(days=days))}}}


def empty_report():
    return {
        "baseline": "test recipe", "activity": None, "errors": [], "new_security_signals": [],
        "new_advisories": [], "bootstrap": False, "run_url": "https://github.com/example/repo/actions/runs/1",
        "sources": [], "security_signals": [], "advisories": [],
    }


class FakeGitHub:
    def __init__(self, fail=None):
        self.fail = fail

    def get(self, path):
        if self.fail and self.fail in path:
            raise monitor.RemoteError("GitHub HTTP 503")
        if "/compare/" in path:
            return {"status": "identical", "ahead_by": 0, "total_commits": 0,
                    "html_url": "https://github.com/example/repo/compare/a...b", "commits": []}
        if "/commits/" in path:
            return commit(2)
        if "/releases/tags/" in path:
            return {"body": "Fixed GHSA-abcd-efgh-ijkl"}
        if path.endswith("/releases/latest"):
            pins = {"openwrt/openwrt": "25.12.5", "Openwrt-Passwall/openwrt-passwall": "26.9.1-1",
                    "vernesong/OpenClash": "v0.47.156", "MetaCubeX/mihomo": "v1.19.30"}
            repo = path.removeprefix("repos/").removesuffix("/releases/latest")
            return {"tag_name": pins.get(repo, "nightly")}
        return {"default_branch": "25.12"}

    def pages(self, path, field=None):
        if self.fail and self.fail in path:
            raise monitor.RemoteError("GitHub HTTP 503")
        if path == "repos/openwrt/openwrt/security-advisories":
            return [{"ghsa_id": "GHSA-abcd-efgh-ijkl", "summary": "An upstream issue", "severity": "high",
                     "html_url": "https://github.com/advisories/GHSA-abcd-efgh-ijkl",
                     "updated_at": monitor.iso(NOW), "vulnerabilities": []}]
        return []


class InactivityTests(unittest.TestCase):
    def test_thresholds_and_timezones(self):
        for days, expected in ((44, 0), (45, 45), (54, 45), (55, 55), (58, 55), (59, 59), (63, 59)):
            with self.subTest(days=days):
                result = monitor.inactivity(commit(days), {}, NOW)
                self.assertEqual(result["days"], days)
                self.assertEqual(result["level"], expected)
        self.assertEqual(monitor.timestamp("2026-09-08T10:00:00+08:00"), NOW)

    def test_observed_new_commit_resets_an_old_committer_timestamp(self):
        previous = {"sha": "b" * 40, "level": 55}
        result = monitor.inactivity(commit(100), previous, NOW)
        self.assertEqual(result["days"], 0)
        same = monitor.inactivity(commit(100), result, NOW + timedelta(days=2))
        self.assertEqual(same["days"], 2)

    def test_alert_once_per_threshold_and_again_for_a_new_commit(self):
        report = empty_report()
        report["activity"] = {**monitor.inactivity(commit(45), {}, NOW), "branch": "25.12"}
        self.assertEqual(len(monitor.notification(report, {}, {}, NOW)), 1)
        previous = {"activity": report["activity"]}
        self.assertEqual(monitor.notification(report, previous, {}, NOW), [])
        report["activity"] = {**monitor.inactivity(commit(55), {}, NOW), "branch": "25.12"}
        self.assertEqual(len(monitor.notification(report, previous, {}, NOW)), 1)
        report["activity"] = {**monitor.inactivity(commit(45, "b" * 40), {}, NOW), "branch": "25.12"}
        self.assertEqual(len(monitor.notification(report, previous, {}, NOW)), 1)

    def test_future_commit_is_not_treated_as_healthy(self):
        with self.assertRaisesRegex(ValueError, "future"):
            monitor.inactivity(commit(-2), {}, NOW)


class ReleaseTests(unittest.TestCase):
    def test_stable_versions_only_and_no_cross_series_or_downgrade(self):
        releases = [
            {"tag_name": "v26.01.1", "prerelease": False},
            {"tag_name": "v25.12.6", "prerelease": True},
            {"tag_name": "v25.12.7", "draft": True},
            {"tag_name": "v25.12.5"}, {"tag_name": "v25.12.4"},
            {"tag_name": "v25.12.9-rc1"}, {"tag_name": "nightly"},
        ]
        self.assertIsNone(monitor.choose_release(releases, "25.12.5", "25.12"))
        releases.append({"tag_name": "v25.12.8"})
        self.assertEqual(monitor.choose_release(releases, "25.12.5", "25.12")["tag_name"], "v25.12.8")
        self.assertEqual(monitor.stable_version("26.9.1-1"), (26, 9, 1, 1))

    def test_current_release_does_not_fetch_older_history(self):
        github = monitor.GitHub("")
        with patch.object(github, "get", return_value={"tag_name": "v1.2.3"}) as get:
            self.assertEqual(monitor.recent_releases(github, "example/repo", "1.2.3"), [{"tag_name": "v1.2.3"}])
            get.assert_called_once_with("repos/example/repo/releases/latest")

    def test_release_history_stops_at_the_pinned_release(self):
        github = monitor.GitHub("")
        releases = [{"tag_name": "v1.2.5"}, {"tag_name": "v1.2.4", "body": "security fix"}, {"tag_name": "v1.2.3"}]
        with patch.object(github, "get", side_effect=[releases[0], releases]) as get:
            self.assertEqual(monitor.recent_releases(github, "example/repo", "1.2.3"), releases)
            self.assertEqual(get.call_count, 2)

    def test_official_feeds_stay_on_the_release_branch(self):
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        for source in monitor.sources(lock):
            if source["name"] == "openwrt" or source["name"] in monitor.OFFICIAL_FEEDS:
                self.assertEqual(source["branch"], "openwrt-25.12")
            else:
                self.assertIsNone(source["branch"])


class ScanTests(unittest.TestCase):
    def test_vendor_failure_marks_scan_incomplete(self):
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        def unavailable():
            raise ValueError("RSS unavailable")
        report, state = monitor.scan(FakeGitHub(), "example/repo", lock, {}, NOW, "test", "", vendor_fetch=unavailable)
        self.assertIn("Tailscale official security RSS", report["errors"][0])
        self.assertNotIn("last_success", state)

    def test_vendor_bulletins_are_included_and_updates_are_deduplicated(self):
        from test_security_vendor import rss
        from security_vendor import parse_tailscale_bulletins
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        fetch = lambda: parse_tailscale_bulletins(rss())
        report, state = monitor.scan(FakeGitHub(), "example/repo", lock, {}, NOW, "test", "", vendor_fetch=fetch)
        self.assertEqual([item["id"] for item in report["new_advisories"]], ["TS-2026-008"])
        report, _ = monitor.scan(FakeGitHub(), "example/repo", lock, state, NOW, "test", "", vendor_fetch=fetch)
        self.assertEqual(report["new_advisories"], [])
    def test_only_unambiguous_distribution_ranges_can_be_excluded(self):
        def advisory(name, value):
            return {"vulnerabilities": [{"package": {"name": name}, "vulnerable_version_range": value}]}
        self.assertTrue(monitor.outside_openwrt_release_range(advisory("OpenWrt", "< 25.12.1"), "25.12.5"))
        self.assertFalse(monitor.outside_openwrt_release_range(advisory("OpenWrt", "<= 25.12.5"), "25.12.5"))
        self.assertFalse(monitor.outside_openwrt_release_range(advisory("OpenWrt", "main"), "25.12.5"))
        self.assertFalse(monitor.outside_openwrt_release_range(advisory("OpenWrt", "< 25.12.6, >=25.12.0"), "25.12.5"))
        self.assertFalse(monitor.outside_openwrt_release_range(advisory("openssl", "< 3.5.1"), "25.12.5"))
        both = advisory("OpenWrt", "<25.12.1")
        both["vulnerabilities"].extend(advisory("OpenWrt", "<=25.12.5")["vulnerabilities"])
        self.assertFalse(monitor.outside_openwrt_release_range(both, "25.12.5"))

    def scan(self, github=None, previous=None):
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        return monitor.scan(github or FakeGitHub(), "example/repo", lock, previous or {}, NOW, "test recipe", "",
                            vendor_fetch=lambda: [])

    def test_baseline_release_reference_is_not_an_affected_device_claim(self):
        report, state = self.scan()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["advisories"][0]["status"], "mentioned_in_baseline_release")
        self.assertEqual(report["new_advisories"], [])
        self.assertEqual(state["last_success"], monitor.iso(NOW))
        self.assertIn("假定设备运行", monitor.render_report(report))

    def test_upstream_failure_does_not_advance_last_complete_check(self):
        previous = {"schema": 1, "last_success": "2026-09-01T00:00:00Z"}
        report, state = self.scan(FakeGitHub("openwrt/packages/security-advisories"), previous)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(state["last_success"], previous["last_success"])
        self.assertIn("检查状态：不完整", monitor.render_report(report))

    def test_advisory_updates_and_new_baselines_are_reconsidered(self):
        class UnresolvedGitHub(FakeGitHub):
            def get(self, path):
                if "/releases/tags/" in path:
                    return {"body": ""}
                return super().get(path)
        report, state = self.scan(UnresolvedGitHub())
        self.assertEqual(len(report["new_advisories"]), 1)
        report, state = self.scan(UnresolvedGitHub(), state)
        self.assertEqual(report["new_advisories"], [])
        state["lock_digest"] = "older baseline"
        report, _ = self.scan(UnresolvedGitHub(), state)
        self.assertTrue(report["baseline_changed"])
        self.assertEqual(len(report["new_advisories"]), 1)

    def test_pagination_collects_all_pages(self):
        github = monitor.GitHub("")
        with patch.object(github, "get", side_effect=[list(range(100)), [100]]) as get:
            self.assertEqual(github.pages("repos/example/repo/releases"), list(range(101)))
            self.assertIn("page=2", get.call_args.args[0])

    def test_query_budget_leaves_time_for_failure_notifications(self):
        github = monitor.GitHub("", budget_seconds=0)
        with patch.object(monitor, "request_json") as request:
            with self.assertRaisesRegex(monitor.RemoteError, "budget"):
                github.get("repos/example/repo")
            request.assert_not_called()


class NotificationTests(unittest.TestCase):
    def test_compressed_api_responses_are_decoded(self):
        response = io.BytesIO(gzip.compress(b'{"ok":true}'))
        response.headers = {"Content-Encoding": "gzip"}
        with patch.object(monitor, "urlopen", return_value=response):
            self.assertEqual(monitor.request_json("https://example.test"), {"ok": True})

    def test_empty_weekly_report_is_silent(self):
        monday = NOW - timedelta(days=1)
        next_state = {}
        messages = monitor.notification(empty_report(), {}, next_state, monday)
        self.assertEqual(messages, [])
        self.assertIn("reported_week", next_state)
        self.assertEqual(monitor.notification(empty_report(), next_state, {}, monday), [])

    def test_historical_high_advisories_and_unreviewed_signals_do_not_notify(self):
        report = empty_report()
        report["bootstrap"] = True
        report["new_advisories"] = [{"id": "GHSA-old", "severity": "critical"}]
        report["new_security_signals"] = [{"title": "uninstalled package security fix"}]
        self.assertEqual(monitor.notification(report, {}, {}, NOW), [])

    def test_only_pending_actions_notify_and_are_deduplicated(self):
        report = empty_report()
        report["actions"] = [{"key": "fix:one", "component": "uhttpd", "title": "Fix request smuggling",
                              "packages": {"uhttpd": "1.0-r1"}, "url": "https://example.test/fix"}]
        state = {}
        messages = monitor.notification(report, {}, state, NOW)
        self.assertEqual(len(messages), 1)
        self.assertIn("uhttpd", messages[0])
        self.assertIn("1.0-r1", messages[0])
        self.assertEqual(monitor.notification(report, state, {}, NOW), [])
        weekly = monitor.notification(report, state, {}, NOW + timedelta(days=6))
        self.assertEqual(len(weekly), 1)
        self.assertIn("每周待处理", weekly[0])
        report["actions"][0]["packages"]["uhttpd"] = "1.0-r2"
        self.assertEqual(len(monitor.notification(report, state, {}, NOW)), 1)

    def test_incomplete_scan_preserves_action_deduplication(self):
        report, state = empty_report(), {}
        report["errors"] = ["inventory temporarily unavailable"]
        monitor.notification(report, {"action_fingerprints": {"old": "hash"}}, state, NOW)
        self.assertEqual(state["action_fingerprints"], {"old": "hash"})

    def test_no_daily_noise_and_force_report(self):
        self.assertEqual(monitor.notification(empty_report(), {}, {}, NOW), [])
        self.assertEqual(len(monitor.notification(empty_report(), {}, {}, NOW, True)), 1)

    def test_manual_zero_actions_report_keeps_unresolved_conditions_visible(self):
        report = empty_report()
        report["advisories"] = [{"id": "TS-test", "status": "configuration_review"}]
        self.assertEqual(monitor.notification(report, {}, {}, NOW), [])
        self.assertIn("1 条公告", monitor.notification(report, {}, {}, NOW, True)[0])

    def test_error_notice_is_deduplicated_and_recovery_is_sent(self):
        report, state = empty_report(), {}
        report["errors"] = ["GitHub HTTP 503"]
        self.assertEqual(len(monitor.notification(report, {}, state, NOW)), 1)
        self.assertEqual(monitor.notification(report, state, {}, NOW), [])
        report["errors"] = []
        self.assertIn("恢复", monitor.notification(report, state, {}, NOW)[0])

    def test_messages_preserve_unicode_and_fit_telegram_limits(self):
        message = "安全\U0001f512" * 2000
        parts = list(monitor.message_chunks(message))
        self.assertEqual("".join(parts), message)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts))

    def test_telegram_retries_do_not_leak_the_token_in_errors(self):
        secret_url = "https://api.telegram.org/bot123:private-test-token/sendMessage"
        with patch.object(monitor, "urlopen", side_effect=URLError(secret_url)), patch.object(monitor.time, "sleep"):
            with self.assertRaises(monitor.RemoteError) as caught:
                monitor.request_json(secret_url, payload={"text": "test"}, telegram=True)
        self.assertNotIn("private-test-token", str(caught.exception))

    def test_telegram_honors_rate_limit_and_checks_api_success(self):
        error = HTTPError("https://example.test", 429, "limited", {}, io.BytesIO(b'{"parameters":{"retry_after":7}}'))
        response = io.BytesIO(b'{"ok":true}')
        with patch.object(monitor, "urlopen", side_effect=[error, response]), patch.object(monitor.time, "sleep") as sleep:
            monitor.request_json("https://example.test", payload={}, telegram=True)
            sleep.assert_called_once_with(7)
        with patch.object(monitor, "urlopen", return_value=io.BytesIO(b'{"ok":false}')):
            with self.assertRaisesRegex(monitor.RemoteError, "rejected"):
                monitor.request_json("https://example.test", payload={}, telegram=True)

    def test_failed_delivery_does_not_acknowledge_the_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            state = output / "state.json"
            state.write_text('{"schema":1}')
            (output / "notification.json").write_text(json.dumps({
                "messages": ["test"], "next_state": {"schema": 1, "delivered": True}, "incomplete": False,
            }))
            command = ["monitor", "notify", "--state", str(state), "--output", str(output)]
            with patch.object(sys, "argv", command), patch.object(monitor, "send_messages", side_effect=monitor.RemoteError("failed")):
                self.assertEqual(monitor.main(), 1)
            self.assertEqual(json.loads(state.read_text()), {"schema": 1})
            with patch.object(sys, "argv", command), patch.object(monitor, "send_messages"):
                self.assertEqual(monitor.main(), 0)
            self.assertTrue(json.loads(state.read_text())["delivered"])

    def test_scan_failure_still_sends_a_failure_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            command = ["monitor", "notify", "--state", str(output / "state.json"), "--output", str(output)]
            with patch.object(sys, "argv", command), patch.object(monitor, "send_messages") as sender:
                self.assertEqual(monitor.main(), 1)
                self.assertIn("未完成", sender.call_args.args[0][0])
            self.assertFalse((output / "state.json").exists())


if __name__ == "__main__":
    unittest.main()

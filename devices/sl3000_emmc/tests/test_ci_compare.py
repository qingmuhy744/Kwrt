from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_compare


def run_fixture(run_id, ended="2026-09-06T03:00:00Z"):
    return {
        "databaseId": run_id,
        "headBranch": "25.12",
        "headSha": "a" * 40,
        "workflowName": "SL-3000 eMMC (Pinned OpenWrt)",
        "jobs": [{
            "name": "build",
            "steps": [{
                "name": "Compile", "status": "completed", "conclusion": "success",
                "startedAt": "2026-09-06T01:00:00Z", "completedAt": ended,
            }],
        }],
    }


class ComparisonTests(unittest.TestCase):
    def test_compares_compile_steps_without_queue_or_upload_time(self):
        baseline = run_fixture(100)
        current = run_fixture(101, "2026-09-06T02:00:00Z")
        baseline["createdAt"] = "2026-09-05T01:00:00Z"
        current["createdAt"] = "2026-09-04T01:00:00Z"
        current["jobs"][0]["steps"].append({"name": "Upload", "status": "in_progress"})
        report = ci_compare.comparison(baseline, current, "owner/repo")
        self.assertIn("2h 00m 00s", report)
        self.assertIn("1h 00m 00s", report)
        self.assertIn("reduced by 1h 00m 00s (50.0%)", report)
        self.assertIn("https://github.com/owner/repo/actions/runs/100", report)
        self.assertIn("Queue", report)

    def test_slower_build_is_not_reported_as_a_speedup(self):
        report = ci_compare.comparison(
            run_fixture(100), run_fixture(101, "2026-09-06T04:00:00Z"), "owner/repo",
        )
        self.assertIn("increased by 1h 00m 00s (50.0%)", report)

    def test_unfinished_failed_missing_or_duplicate_compile_is_rejected(self):
        for change in ("unfinished", "failed", "missing", "duplicate", "negative", "zero"):
            run = run_fixture(100)
            steps = run["jobs"][0]["steps"]
            if change == "unfinished":
                steps[0]["status"] = "in_progress"
            elif change == "failed":
                steps[0]["conclusion"] = "failure"
            elif change == "missing":
                steps.clear()
            elif change == "duplicate":
                steps.append(deepcopy(steps[0]))
            elif change == "negative":
                steps[0]["completedAt"] = "2026-09-06T00:00:00Z"
            else:
                steps[0]["completedAt"] = steps[0]["startedAt"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                ci_compare.compile_seconds(run)

    def test_different_workflow_branch_or_same_run_is_rejected(self):
        baseline = run_fixture(100)
        for key, value in (("workflowName", "other"), ("headBranch", "main"), ("databaseId", 100)):
            current = run_fixture(101)
            current[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                ci_compare.comparison(baseline, current, "owner/repo")

    def test_run_ids_are_validated_before_calling_github(self):
        with patch.object(ci_compare.subprocess, "check_output") as github:
            for value in ("", "0", "-1", "1;false", "--help"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ci_compare.fetch_run("owner/repo", value)
            github.assert_not_called()

    def test_fetch_uses_read_only_github_command_with_timeout(self):
        with patch.object(ci_compare.subprocess, "check_output", return_value="{}") as github:
            self.assertEqual(ci_compare.fetch_run("owner/repo", "100"), {})
        command = github.call_args.args[0]
        self.assertEqual(command[:6], ["gh", "run", "view", "100", "--repo", "owner/repo"])
        self.assertEqual(github.call_args.kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()

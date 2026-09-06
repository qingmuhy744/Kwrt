#!/usr/bin/env python3
"""Compare successful Compile steps without counting queue or upload time."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess


def fetch_run(repository, run_id):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        raise ValueError("Comparison run ID must be a positive integer")
    output = subprocess.check_output([
        "gh", "run", "view", str(run_id), "--repo", repository,
        "--json", "databaseId,headBranch,headSha,workflowName,jobs",
    ], text=True, timeout=30)
    return json.loads(output)


def compile_seconds(run):
    jobs = [job for job in run["jobs"] if job["name"] == "build"]
    if len(jobs) != 1:
        raise ValueError("Expected exactly one build job")
    steps = [step for step in jobs[0]["steps"] if step["name"] == "Compile"]
    if len(steps) != 1 or steps[0]["status"] != "completed" or steps[0]["conclusion"] != "success":
        raise ValueError("Comparison requires a completed, successful Compile step")
    step = steps[0]
    start = datetime.fromisoformat(step["startedAt"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(step["completedAt"].replace("Z", "+00:00"))
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise ValueError("Compile duration must be positive")
    return seconds


def duration(seconds):
    hours, remainder = divmod(round(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


def comparison(baseline, current, repository):
    if baseline["databaseId"] == current["databaseId"]:
        raise ValueError("Baseline and current run must be different")
    if any(baseline[key] != current[key] for key in ("workflowName", "headBranch")):
        raise ValueError("Baseline must use the same workflow and branch")
    before, after = compile_seconds(baseline), compile_seconds(current)
    change = before - after
    direction = "reduced" if change >= 0 else "increased"
    lines = [
        "### Compile-time comparison", "",
        "| Run | Recipe commit | Compile wall time |",
        "| --- | --- | --- |",
    ]
    for label, run, seconds in (("Baseline", baseline, before), ("Current", current, after)):
        run_id = run["databaseId"]
        url = f"https://github.com/{repository}/actions/runs/{run_id}"
        lines.append(f"| [{label} {run_id}]({url}) | `{run['headSha'][:12]}` | {duration(seconds)} |")
    lines += [
        "", f"Compile time {direction} by {duration(abs(change))} ({abs(change) / before:.1%}).", "",
        "Queue, source download, verification and upload time are excluded.",
        "Check runner resources, cache restores and build profiles before attributing the difference to YJIT.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run", required=True)
    parser.add_argument("--current-run", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    try:
        report = comparison(
            fetch_run(args.repository, args.baseline_run),
            fetch_run(args.repository, args.current_run),
            args.repository,
        )
        print(report)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
                stream.write(report)
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Build comparison unavailable: {error}\n")

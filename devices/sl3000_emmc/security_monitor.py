#!/usr/bin/env python3
"""Watch pinned upstream sources and notify Telegram without changing firmware."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
OFFICIAL_FEEDS = {"packages", "luci", "routing", "telephony", "video"}
ADVISORY_REPOS = (
    "openwrt/openwrt", "openwrt/luci", "openwrt/packages", "openwrt/odhcpd",
    "openwrt/uhttpd", "openwrt/rpcd", "openwrt/cgi-io", "openwrt/ubus",
    "openwrt/netifd", "openwrt/mdnsd", "openssl/openssl", "curl/curl",
    "Openwrt-Passwall/openwrt-passwall", "Openwrt-Passwall/openwrt-passwall-packages",
    "vernesong/OpenClash", "MetaCubeX/mihomo", "XTLS/Xray-core",
    "SagerNet/sing-box", "tailscale/tailscale",
)
EXTRA_RELEASES = {
    "XTLS/Xray-core": "xray-core (passwall_packages)",
    "SagerNet/sing-box": "sing-box (passwall_packages)",
    "tailscale/tailscale": "tailscale (packages)",
}
SECURITY_WORDS = re.compile(
    r"CVE-\d{4}-\d{4,}|GHSA-[a-z0-9-]+|\bsecurity\b|\bvulnerabilit(?:y|ies)\b|"
    r"\b(?:use.after.free|buffer overflow|remote code execution)\b|漏洞|安全修复",
    re.I,
)
GHSA = re.compile(r"GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}", re.I)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class RemoteError(RuntimeError):
    pass


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def repository_name(url):
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)\.git", url)
    if not match:
        raise ValueError("Unexpected source repository URL")
    return match[1]


def stable_version(tag):
    if not isinstance(tag, str) or not re.fullmatch(r"v?\d+(?:\.\d+)+(?:-\d+)?", tag):
        return None
    return tuple(int(part) for part in re.split(r"[.-]", tag.removeprefix("v")))


def request_json(url, token="", payload=None, telegram=False):
    headers = {"User-Agent": "sl3000-security-monitor", "Accept": "application/json" if telegram else "application/vnd.github+json",
               "Accept-Encoding": "gzip"}
    if token:
        headers["Authorization"] = "Bearer " + token
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    service = "Telegram" if telegram else "GitHub"
    for attempt in range(3):
        delay = 2 ** (attempt + 1)
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=25) as response:
                body = response.read()
                if getattr(response, "headers", {}).get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                result = json.loads(body)
            if telegram and (not isinstance(result, dict) or result.get("ok") is not True):
                raise RemoteError("Telegram rejected the message")
            return result
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if error.code == 429:
                try:
                    delay = max(delay, int(json.load(error).get("parameters", {}).get("retry_after", delay)))
                except (ValueError, TypeError, AttributeError):
                    pass
            if not retryable or attempt == 2 or delay > 30:
                raise RemoteError(f"{service} HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError):
            if attempt == 2:
                raise RemoteError(f"{service} connection failed after retries") from None
        except (ValueError, TypeError):
            raise RemoteError(f"{service} returned invalid JSON") from None
        time.sleep(delay)
    raise RemoteError(f"{service} request failed")


class GitHub:
    def __init__(self, token, budget_seconds=600):
        self.token = token
        self.deadline = time.monotonic() + budget_seconds

    def get(self, path):
        if not path.startswith("repos/"):
            raise ValueError("Unexpected GitHub API path")
        if time.monotonic() >= self.deadline:
            raise RemoteError("GitHub query time budget exhausted; coverage incomplete")
        return request_json("https://api.github.com/" + path, self.token)

    def pages(self, path, field=None):
        items = []
        for page in range(1, 11):
            separator = "&" if "?" in path else "?"
            response = self.get(f"{path}{separator}per_page=100&page={page}")
            batch = response[field] if field else response
            if not isinstance(batch, list):
                raise ValueError("Unexpected GitHub collection")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise RemoteError("GitHub pagination exceeded 1000 entries; coverage incomplete")


def sources(lock):
    series = ".".join(lock["openwrt"]["release"].split(".")[:2])
    result = []
    for name, source in [("openwrt", lock["openwrt"]), *lock["feeds"].items()]:
        result.append({
            "name": name, "repository": repository_name(source["url"]),
            "commit": source["commit"], "release": source.get("release"),
            "branch": "openwrt-" + series if name == "openwrt" or name in OFFICIAL_FEEDS else None,
        })
    return result


def choose_release(releases, current=None, series=None):
    eligible = []
    for release in releases:
        version = stable_version(release.get("tag_name"))
        if release.get("draft") or release.get("prerelease") or version is None:
            continue
        if series and version[:2] != tuple(map(int, series.split("."))):
            continue
        eligible.append((version, release))
    if not eligible:
        return None
    version, release = max(eligible, key=lambda item: item[0])
    if current is not None:
        pinned = stable_version(current)
        if pinned is None:
            raise ValueError("Pinned release has an unsupported version format")
        if version <= pinned:
            return None
    return release


def recent_releases(github, repo, current):
    latest = github.get(f"repos/{repo}/releases/latest")
    if current is None or stable_version(latest.get("tag_name")) == stable_version(current):
        return [latest]
    releases = []
    for page in range(1, 11):
        batch = github.get(f"repos/{repo}/releases?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("Unexpected GitHub release collection")
        releases.extend(batch)
        if any(stable_version(item.get("tag_name")) == stable_version(current) for item in batch):
            return releases
        if len(batch) < 100:
            break
    raise RemoteError("Pinned release missing from bounded release history; coverage incomplete")


def outside_openwrt_release_range(advisory, release):
    current = stable_version(release)
    vulnerabilities = advisory.get("vulnerabilities") or []
    if current is None or not vulnerabilities:
        return False
    # Only accept explicit single-bound OpenWrt release ranges. Package versions,
    # branch names, commit ranges and compound constraints remain unclassified.
    for item in vulnerabilities:
        if item.get("package", {}).get("name", "").lower() != "openwrt":
            return False
        match = re.fullmatch(r"\s*(<=|>=|<|>|==|=)\s*(v?\d+\.\d+\.\d+)\s*",
                             item.get("vulnerable_version_range") or "")
        if not match:
            return False
        bound = stable_version(match[2])
        affected = {"<": current < bound, "<=": current <= bound, ">": current > bound,
                    ">=": current >= bound, "=": current == bound, "==": current == bound}
        if affected[match[1]]:
            return False
    return True


def inactivity(commit, previous, now):
    committed = timestamp(commit["commit"]["committer"]["date"])
    if committed > now + timedelta(days=1):
        raise ValueError("Latest commit timestamp is in the future")
    observed = previous.get("observed_at")
    if previous.get("sha") and previous["sha"] != commit["sha"]:
        observed = iso(now)
    reference = max(committed, timestamp(observed)) if observed else committed
    days = max(0, (now - reference).days)
    level = 59 if days >= 59 else 55 if days >= 55 else 45 if days >= 45 else 0
    return {
        "sha": commit["sha"], "committed_at": iso(committed), "observed_at": observed,
        "days": days, "level": level, "estimated_stop": iso(reference + timedelta(days=60)),
    }


def load_state(path):
    if not path.exists():
        return {"schema": 1}
    state = json.loads(path.read_text())
    if state.get("schema") != 1:
        raise ValueError("Unsupported monitor state schema")
    return state


def scan(github, repository, lock, previous, now, baseline, run_url, progress=False):
    report = {
        "schema": 1, "checked_at": iso(now), "repository": repository,
        "baseline": baseline, "lock_digest": digest(lock), "run_url": run_url,
        "sources": [], "releases": [], "security_signals": [], "advisories": [],
        "errors": [], "activity": None, "new_advisories": [], "new_security_signals": [],
    }
    next_state = json.loads(json.dumps(previous))
    next_state.setdefault("schema", 1)
    report["baseline_changed"] = bool(previous.get("lock_digest") and previous["lock_digest"] != report["lock_digest"])
    if report["baseline_changed"]:
        next_state.pop("advisories", None)
        next_state.pop("signals", None)
    next_state["lock_digest"] = report["lock_digest"]
    seen_advisories = next_state.setdefault("advisories", {})
    seen_signals = next_state.setdefault("signals", {})

    def attempt(label, operation):
        if progress:
            print("Checking " + label, flush=True)
        try:
            return operation()
        except (RemoteError, ValueError, KeyError, TypeError) as error:
            report["errors"].append(f"{label}: {error}")
            return None

    def add_signal(key, component, title, url, evidence=""):
        signal = {"key": key, "component": component, "title": title, "url": url,
                  "evidence_digest": digest(evidence)}
        report["security_signals"].append(signal)
        fingerprint = digest(signal)
        if seen_signals.get(key) != fingerprint:
            report["new_security_signals"].append(signal)
        seen_signals[key] = fingerprint

    def check_activity():
        metadata = github.get(f"repos/{repository}")
        branch = metadata["default_branch"]
        commit = github.get(f"repos/{repository}/commits/{quote(branch, safe='')}")
        report["activity"] = inactivity(commit, previous.get("activity", {}), now)
        report["activity"]["branch"] = branch
        next_state["activity"] = report["activity"]

    attempt("repository activity", check_activity)
    def check_release(repo, current, series=None):
        releases = recent_releases(github, repo, current)
        release = choose_release(releases, current, series)
        if release:
            entry = {"repository": repo, "current": current, "latest": release["tag_name"],
                     "url": release["html_url"], "published_at": release["published_at"]}
            report["releases"].append(entry)
            for candidate in releases:
                if choose_release([candidate], current, series) and SECURITY_WORDS.search(candidate.get("body") or ""):
                    add_signal("release:" + repo + ":" + candidate["tag_name"], repo,
                               "Release notes mention security fixes: " + candidate["tag_name"], candidate["html_url"],
                               candidate.get("body") or "")

    for source in sources(lock):
        def check_source(source=source):
            repo, branch = source["repository"], source["branch"]
            if not branch:
                branch = github.get(f"repos/{repo}")["default_branch"]
            target = github.get(f"repos/{repo}/commits/{quote(branch, safe='')}")["sha"]
            comparison = f"repos/{repo}/compare/{source['commit']}...{target}"
            summary = github.get(comparison + "?per_page=100&page=1")
            if summary["status"] not in ("ahead", "identical"):
                raise ValueError("Pinned source is not an ancestor of the monitored branch")
            report["sources"].append({**source, "branch": branch, "head": target,
                                       "ahead_by": summary["ahead_by"], "url": summary["html_url"]})
            commits = summary["commits"]
            if summary["total_commits"] > len(commits):
                commits = github.pages(comparison, "commits")
            if len(commits) != summary["total_commits"]:
                raise ValueError("Incomplete comparison commit list")
            for commit in commits:
                message = commit["commit"]["message"]
                if SECURITY_WORDS.search(message):
                    add_signal("commit:" + repo + ":" + commit["sha"], source["name"],
                               message.splitlines()[0], commit["html_url"], message)
        attempt(source["name"] + " source", check_source)
        if source["release"]:
            series = ".".join(lock["openwrt"]["release"].split(".")[:2]) if source["name"] == "openwrt" else None
            attempt(source["name"] + " releases", lambda source=source, series=series:
                    check_release(source["repository"], source["release"], series))
    attempt("mihomo releases", lambda: check_release("MetaCubeX/mihomo", lock["mihomo"]["version"]))

    # Feed commits do not identify the installed Xray/sing-box/Tailscale versions.
    # Their release records are reported as leads, never as proof of exposure.
    for repo in EXTRA_RELEASES:
        attempt(repo + " releases", lambda repo=repo: check_release(repo, None))

    fixed_by_baseline = set()
    pinned_release = attempt("OpenWrt baseline release notes", lambda: github.get(
        "repos/openwrt/openwrt/releases/tags/v" + quote(lock["openwrt"]["release"], safe="")))
    if pinned_release:
        fixed_by_baseline.update(GHSA.findall(pinned_release.get("body") or ""))

    for repo in ADVISORY_REPOS:
        def check_advisories(repo=repo):
            for advisory in github.pages(f"repos/{repo}/security-advisories"):
                if advisory.get("withdrawn_at"):
                    continue
                key = advisory["ghsa_id"]
                status = "needs_review"
                if key in fixed_by_baseline and repo.startswith("openwrt/"):
                    status = "mentioned_in_baseline_release"
                elif repo.startswith("openwrt/") and outside_openwrt_release_range(advisory, lock["openwrt"]["release"]):
                    status = "outside_declared_release_range"
                item = {"id": key, "repository": repo, "severity": advisory.get("severity", "unknown"),
                        "title": advisory["summary"], "url": advisory["html_url"],
                        "updated_at": advisory["updated_at"], "status": status,
                        "vulnerabilities": advisory.get("vulnerabilities", [])}
                report["advisories"].append(item)
                fingerprint = digest(item)
                if seen_advisories.get(key) != fingerprint and status == "needs_review":
                    report["new_advisories"].append(item)
                seen_advisories[key] = fingerprint
        attempt(repo + " advisories", check_advisories)

    report["advisories"].sort(key=lambda item: item["updated_at"], reverse=True)
    report["new_advisories"].sort(key=lambda item: item["updated_at"], reverse=True)
    next_state["last_attempt"] = iso(now)
    if not report["errors"]:
        next_state["last_success"] = iso(now)
    report["last_success"] = next_state.get("last_success")
    report["bootstrap"] = not previous.get("last_attempt")
    return report, next_state


def plain(value, limit=240):
    return " ".join(str(value).split())[:limit]


def markdown(value):
    return plain(value).replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(
        ">", "&gt;").replace("[", "\\[").replace("]", "\\]").replace("`", "'")


def render_report(report):
    lines = ["# SL3000 安全与版本检查", "", f"检查时间：{report['checked_at']}",
             f"基准：{markdown(report['baseline'])}", "",
             "按约定，假定设备运行当前配方构建的固件。用匹配配方的构建包清单筛选安全修复。",
             "待处理表示已包含组件缺少上游安全修复，不表示所有漏洞触发条件都已在设备上验证。",
             "无法确定适用性、已有本地补丁需复核的项目只保留在完整报告，不发送漏洞告警。", "",
             f"检查状态：{'不完整' if report['errors'] else '已完成所列数据源检查'}",
             f"最近完整检查：{report.get('last_success') or '尚无'}", ""]
    inventory = report.get("inventory")
    if inventory:
        lines += [f"包清单：构建 [{inventory['workflow_run']}](https://github.com/{report['repository']}/actions/runs/{inventory['workflow_run']})，"
                  f"{len(inventory['packages'])} 个包，已核对固件配方输入一致。", ""]
    lines += ["## 需要处理的组件安全修复", "", "| 组件 | 构建中的版本 | 缺少的安全修复 |", "| --- | --- | --- |"]
    for action in report.get("actions", []):
        versions = ", ".join(sorted(set(action["packages"].values())))
        lines += [f"| {markdown(action['component'])} | {markdown(versions)} | [{markdown(action['title'])}]({action['url']}) |"]
    if not report.get("actions"):
        lines += ["", "本次未确认需纳入的组件修复。检查失败或待核实项目不能视为已排除漏洞。"]
    lines += ["", "处理方式：将所列补丁或安全版本纳入锁定源码，重新构建、验证并刷入固件。", ""]
    if report.get("assessment_notes"):
        lines += ["## 适用性核实限制", "", *["- " + markdown(note) for note in report["assessment_notes"]], ""]
    if report["errors"]:
        lines += ["## 未完成的检查", "", *["- " + markdown(error) for error in report["errors"]], ""]
    activity = report.get("activity")
    if activity:
        lines += ["## 定时任务停用提醒", "",
                  f"默认分支 `{activity['branch']}` 最近提交：{activity['committed_at']}，估算 {activity['days']} 天无提交。",
                  f"60 天参考时间：{activity['estimated_stop']}。在第 45、55、59 天档位提醒，周报持续列出状态。",
                  "GitHub 按仓库活动判断停用，此处用提交与观察记录保守估算，不是平台的精确倒计时。",
                  "任务一旦停用就不能再自行发送提醒，需要在 Actions 页面重新启用；本任务不会自动创建保活提交。", ""]
    lines += ["## 锁定源码与监测分支", "", "| 来源 | 锁定提交 | 分支 | 新增提交 | 对比 |",
              "| --- | --- | --- | --- | --- |"]
    for source in report["sources"]:
        lines.append(f"| {markdown(source['name'])} | `{source['commit'][:12]}` | {markdown(source['branch'])} | "
                     f"{source['ahead_by']} | [查看]({source['url']}) |")
    lines += ["", "## 上游正式版本", "", "| 来源 | 锁定版本 | 上游版本 | 说明 |", "| --- | --- | --- | --- |"]
    for release in report["releases"]:
        current = release["current"] or "具体包版本未核实"
        lines.append(f"| {markdown(release['repository'])} | {markdown(current)} | {markdown(release['latest'])} | "
                     f"[发布说明]({release['url']}) |")
    lines += ["", "## 安全修复线索（待核实）", ""]
    for signal in report["security_signals"]:
        state = {"not_installed": "未安装", "scope_excluded": "功能范围已排除",
                 "fix_pending": "需要纳入修复"}.get(signal.get("assessment"), "待核实，不推送")
        lines.append(f"- **{markdown(signal['component'])}** [{state}]：[{markdown(signal['title'])}]({signal['url']})")
    if not report["security_signals"]:
        lines.append("本次未在监测范围内发现安全修复关键词线索；这不代表固件不存在漏洞。")
    lines += ["", "## 公开安全公告", "", "首次检查包含历史公告；需要结合实际包及回移补丁复核。", "",
              "| 公告 | 来源 | 严重度 | 状态 | 标题 |", "| --- | --- | --- | --- | --- |"]
    for item in report["advisories"]:
        status = {"mentioned_in_baseline_release": "基线发布说明已列为修复",
                  "outside_declared_release_range": "锁定版本在公告声明范围外",
                  "not_installed": "未安装相关包", "other_platform": "不适用于 Linux",
                  "fixed_in_inventory": "构建版本已含修复", "fix_pending": "需要纳入修复"}.get(item["status"], "待核实，不推送")
        lines.append(f"| [{item['id']}]({item['url']}) | {markdown(item['repository'])} | {markdown(item['severity'])} | "
                     f"{status} | {markdown(item['title'])} |")
    lines += ["", "## 覆盖范围", "", "- 公告来源：" + ", ".join(ADVISORY_REPOS),
              "- 另外检查锁文件中各源码分支的后续提交，以及 OpenWrt 同系列和代理组件的正式版本说明。",
              "- 推送需要包清单与配方匹配、组件已包含、后续提交明确涉及安全修复，并验证源码路径。",
              "- 额外跟踪 uhttpd、cgi-io、odhcpd、rpcd、ubus、netifd、umdns 的精确上游提交；本地补丁需另行核实。",
              "- 未覆盖所有厂商公告、邮件列表、静态/传递依赖和未公开漏洞；未知组件映射只进报告。",
              "- 检测不会更新锁文件、构建、发布或刷写固件；无新增线索不等于安全认证。", ""]
    return "\n".join(lines)


def notification(report, previous, next_state, now, force=False):
    messages = []
    week = now.astimezone(SHANGHAI).strftime("%G-W%V")
    weekly = now.astimezone(SHANGHAI).weekday() == 0 and previous.get("reported_week") != week
    activity = report.get("activity")
    old_activity = previous.get("activity", {})
    if activity and activity["level"] and (activity["sha"], activity["level"]) != (
            old_activity.get("sha"), old_activity.get("level")):
        messages.append(
            f"SL3000 定时任务停用预警\n默认分支：{activity['branch']}\n"
            f"估算已 {activity['days']} 天无提交，60 天参考日期：{activity['estimated_stop'][:10]}。\n"
            "请安排实际维护提交；若任务已停用，需要在 GitHub Actions 页面重新启用。\n"
            "这是按提交记录估算，GitHub 实际依据仓库活动判断。停用后机器人无法继续提醒。"
        )
    errors = report["errors"]
    error_key = digest(errors) if errors else ""
    if errors and error_key != previous.get("error_digest"):
        messages.append("SL3000 检查不完整\n" + "\n".join(plain(error) for error in errors[:5]) +
                        f"\n失败数据源：{len(errors)}；不能据此判断没有安全更新。")
    elif not errors and previous.get("error_digest"):
        messages.append("SL3000 检查恢复：本次已完成所列数据源检查。")
    next_state["error_digest"] = error_key

    actions = report.get("actions", [])
    old_actions = previous.get("action_fingerprints", {})
    fingerprints = {item["key"]: digest(item) for item in actions}
    changed = [item for item in actions if old_actions.get(item["key"]) != fingerprints[item["key"]]]
    # Incomplete scans must not clear observations for temporarily missing sources.
    next_state["action_fingerprints"] = {**old_actions, **fingerprints} if errors else fingerprints
    selected = actions if force or weekly else changed
    if selected:
        groups = {}
        for action in selected:
            groups.setdefault(action["component"], []).append(action)
        label = "SL3000 待处理的安全修复" if force or not weekly else "SL3000 每周待处理修复"
        body = [label, f"基准：{plain(report['baseline'], 160)}"]
        for component, items in groups.items():
            versions = ", ".join(sorted({value for item in items for value in item["packages"].values()}))
            body += [f"\n{plain(component)} | 当前 {plain(versions, 120)} | {len(items)} 项修复"]
            for item in items[:3]:
                body += [plain(item["title"], 160), item["url"]]
            if len(items) > 3:
                body += [f"其余 {len(items) - 3} 项见完整报告。"]
        body += ["\n处理：纳入所列安全补丁或安全版本，重新构建、验证并刷入。"]
        if weekly and activity:
            body += [f"仓库估算 {activity['days']} 天无提交；45/55/59 天会另发停用预警。"]
        if errors:
            body += ["本次部分检查失败，以上仅为已核实项目。"]
        messages.append("\n".join(body))
    elif force:
        messages.append("SL3000 手动检查结果\n" + ("检查不完整，不能判断是否没有待处理修复。" if errors else
                        "本次监测范围内没有已确认需纳入的组件安全修复。") + "\n基准：" + plain(report["baseline"]))
    if weekly:
        next_state["reported_week"] = week

    suffix = "\n完整报告与运行记录：\n" + report["run_url"] if report["run_url"] else ""
    return [message + suffix for message in messages]


def message_chunks(message, limit=3500):
    chunk, units = [], 0
    for char in message:
        size = len(char.encode("utf-16-le")) // 2
        if units + size > limit:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(char)
        units += size
    if chunk:
        yield "".join(chunk)


def send_messages(messages, token, chat_id):
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token or ""):
        raise ValueError("Missing or invalid TELEGRAM_TOKEN")
    if not re.fullmatch(r"-?\d+", chat_id or ""):
        raise ValueError("Missing or invalid TELEGRAM_CHAT_ID")
    for message in messages:
        for chunk in message_chunks(message):
            request_json("https://api.telegram.org/bot" + token + "/sendMessage", telegram=True,
                         payload={"chat_id": chat_id, "text": chunk, "link_preview_options": {"is_disabled": True}})


def baseline_lock(github, repository, recipe):
    if recipe:
        if not re.fullmatch(r"[0-9a-f]{40}", recipe):
            raise ValueError("SL3000_DEPLOYED_RECIPE must be a full recipe commit SHA")
        path = "devices/sl3000_emmc/sources.lock.json"
        content = github.get(f"repos/{repository}/contents/{path}?" + urlencode({"ref": recipe}))
        lock = json.loads(base64.b64decode(content["content"], validate=False))
        label = f"约定运行配方 {recipe[:12]} 的锁定源码"
    else:
        lock = json.loads((HERE / "sources.lock.json").read_text())
        label = "按约定运行 GitHub 当前默认分支配方的锁定源码"
    from prepare import validate_lock
    validate_lock(lock)
    return lock, label


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "notify"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "qingmuhy744/Kwrt"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force-report", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository):
        parser.error("Invalid repository")
    try:
        if args.command == "notify":
            notification_file = args.output / "notification.json"
            if not notification_file.exists():
                url = f"https://github.com/{args.repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
                send_messages(["SL3000 安全检查未完成，检查程序未能生成报告。请查看运行日志：\n" + url],
                              os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
                return 1
            bundle = json.loads(notification_file.read_text())
            send_messages(bundle["messages"], os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
            args.state.parent.mkdir(parents=True, exist_ok=True)
            args.state.write_text(json.dumps(bundle["next_state"], ensure_ascii=True, indent=2) + "\n")
            if os.environ.get("GITHUB_OUTPUT"):
                with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                    stream.write("state_ready=true\n")
            print(f"Telegram notification batches delivered: {len(bundle['messages'])}")
            return 1 if bundle["incomplete"] else 0

        now = datetime.now(timezone.utc)
        github = GitHub(os.environ.get("GH_TOKEN", ""))
        previous = load_state(args.state)
        lock, baseline = baseline_lock(github, args.repository, os.environ.get("SL3000_DEPLOYED_RECIPE", ""))
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        run_url = f"https://github.com/{args.repository}/actions/runs/{run_id}" if run_id.isdecimal() else ""
        report, next_state = scan(github, args.repository, lock, previous, now, baseline, run_url, progress=True)
        try:
            from security_inventory import load as load_inventory
            from security_assessment import assess
            inventory = load_inventory(github, args.repository, lock, previous, os.environ.get("SL3000_DEPLOYED_RECIPE", ""))
            next_state["inventory"] = inventory
            print("Assessing installed components against matching build inventory", flush=True)
            assess(github, lock, inventory, report)
        except (RemoteError, ValueError, KeyError, TypeError, OSError) as error:
            report["errors"].append(f"Package applicability: {error}")
        if report["errors"]:
            report["last_success"] = previous.get("last_success")
            next_state["last_success"] = previous.get("last_success")
        messages = notification(report, previous, next_state, now, args.force_report)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        rendered = render_report(report)
        (args.output / "report.md").write_text(rendered)
        (args.output / "notification.json").write_text(json.dumps(
            {"messages": messages, "next_state": next_state, "incomplete": bool(report["errors"])}, ensure_ascii=False))
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
                stream.write(rendered)
        print(f"Sources: {len(report['sources'])}; advisories: {len(report['advisories'])}; incomplete checks: {len(report['errors'])}")
        return 0
    except (RemoteError, ValueError, KeyError, TypeError, OSError) as error:
        print(f"Security monitor failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

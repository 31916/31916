#!/usr/bin/env python3
"""Render public GitHub language and commit-time statistics without dependencies."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


UTC = timezone.utc
JST = timezone(timedelta(hours=9))
DAYS = 90
ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "2022-11-28"
COLORS = {"background": "#f6f8ff", "text": "#183153", "muted": "#536888", "bar": "#3478f6", "grid": "#dce5f5", "track": "#e8eef9"}


class ApiError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        self.api_message = message
        super().__init__(f"GitHub API request failed (HTTP {status or 'unknown'}): {message}")


class GitHubApi:
    """Use the workflow token or the user's existing GitHub CLI authentication."""

    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN")

    def get(self, endpoint: str):
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError("Expected a GitHub API path")
        if self.token:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "31916-profile-statistics",
            }
            request = Request("https://api.github.com" + endpoint, headers=headers)
            try:
                with urlopen(request, timeout=45) as response:
                    return json.load(response)
            except HTTPError as error:
                try:
                    message = json.load(error).get("message", "Request rejected")
                except (ValueError, AttributeError):
                    message = "Request rejected"
                raise ApiError(error.code, str(message)) from None
            except URLError:
                raise ApiError(None, "Connection failed") from None
        try:
            result = subprocess.run(
                ["gh", "api", "--method", "GET", endpoint,
                 "-H", "Accept: application/vnd.github+json",
                 "-H", f"X-GitHub-Api-Version: {API_VERSION}"],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        except FileNotFoundError:
            raise RuntimeError("Set GITHUB_TOKEN or install and authenticate GitHub CLI.") from None
        if result.returncode:
            status_match = re.search(r"HTTP (\d{3})", result.stderr)
            status = int(status_match.group(1)) if status_match else None
            try:
                message = json.loads(result.stdout).get("message", "Request rejected")
            except (ValueError, AttributeError):
                message = "Request rejected; check GitHub CLI authentication and connectivity"
            raise ApiError(status, str(message))
        return json.loads(result.stdout)

    def pages(self, path: str, parameters: dict | None = None):
        parameters = dict(parameters or {})
        parameters["per_page"] = 100
        page = 1
        while True:
            parameters["page"] = page
            rows = self.get(path + "?" + urlencode(parameters))
            if not isinstance(rows, list):
                raise ValueError(f"Expected an array from {path}")
            yield from rows
            if len(rows) < 100:
                return
            page += 1


def parse_time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Timestamps must include a time zone")
    return timestamp.astimezone(UTC)


def iso_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def eligible_repository(repository: dict, owner: str) -> bool:
    return (
        repository.get("owner", {}).get("login", "").casefold() == owner.casefold()
        and repository.get("private") is False
        and repository.get("visibility", "public") == "public"
        and repository.get("fork") is False
        and repository.get("name", "").casefold() != owner.casefold()
    )


def aggregate_languages(repository_languages: list[dict]) -> list[dict]:
    totals: Counter = Counter()
    for languages in repository_languages:
        for name, count in languages.items():
            if not isinstance(name, str) or type(count) is not int or count < 0:
                raise ValueError("Invalid language byte count")
            if count:
                totals[name] += count
    denominator = sum(totals.values())
    return [
        {"name": name, "bytes": count, "share_percent": count * 100 / denominator}
        for name, count in sorted(totals.items(), key=lambda item: (-item[1], item[0].casefold()))
    ]


def aggregate_activity(commits: list[dict], owner: str, start: datetime, end: datetime) -> dict:
    hours = [0] * 24
    weekdays = [0] * 7
    seen: set[str] = set()
    active_dates: set[str] = set()
    for commit in commits:
        author = commit.get("author") or {}
        if author.get("login", "").casefold() != owner.casefold() or author.get("type") == "Bot":
            continue
        if len(commit.get("parents", [])) > 1:
            continue
        timestamp_value = (commit.get("commit", {}).get("author") or {}).get("date")
        if not timestamp_value:
            raise ValueError("An attributed commit is missing its author timestamp")
        authored = parse_time(timestamp_value)
        if not start <= authored <= end:
            continue
        sha = commit.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise ValueError("Invalid commit identifier")
        if sha in seen:
            continue
        seen.add(sha)
        local_time = authored.astimezone(JST)
        hours[local_time.hour] += 1
        weekdays[local_time.weekday()] += 1
        active_dates.add(local_time.date().isoformat())
    return {
        "commits": len(seen),
        "active_days": len(active_dates),
        "hourly_commits_jst": hours,
        "weekday_commits_jst": weekdays,
    }


def collect(api: GitHubApi, owner: str, now: datetime) -> dict:
    now = now.astimezone(UTC).replace(microsecond=0)
    start = now - timedelta(days=DAYS)
    owner_path = quote(owner, safe="")
    repositories = sorted(
        (repo for repo in api.pages(f"/users/{owner_path}/repos", {"type": "owner", "sort": "full_name"})
         if eligible_repository(repo, owner)),
        key=lambda repo: repo["name"].casefold(),
    )
    language_maps = []
    commits = []
    source_repositories = []
    for repository in repositories:
        name = repository["name"]
        path = f"/repos/{owner_path}/{quote(name, safe='')}"
        languages = api.get(path + "/languages")
        if not isinstance(languages, dict):
            raise ValueError("Expected language byte counts")
        language_maps.append(languages)
        # GitHub's since/until filters use last-updated dates, which need not
        # equal author dates. Fetch all author-filtered history, then bound the
        # author timestamp locally so amended/imported commits are not missed.
        parameters = {"author": owner}
        if repository.get("default_branch"):
            parameters["sha"] = repository["default_branch"]
        empty = False
        try:
            commits.extend(api.pages(path + "/commits", parameters))
        except ApiError as error:
            if error.status == 409 and error.api_message.casefold().rstrip(".") == "git repository is empty":
                empty = True
            else:
                raise
        source_repositories.append({
            "name": name,
            "url": f"https://github.com/{owner}/{name}",
            "default_branch": repository.get("default_branch"),
            "archived": bool(repository.get("archived", False)),
            "empty": empty,
            "language_bytes": dict(sorted(languages.items())),
        })
    languages = aggregate_languages(language_maps)
    return {
        "schema_version": 1,
        "owner": owner,
        "generated_at": iso_time(now),
        "window": {"days": DAYS, "from": iso_time(start), "to": iso_time(now), "timezone": "Asia/Tokyo (UTC+09:00)"},
        "scope": {
            "visibility": "public",
            "ownership": "owned",
            "forks": "excluded",
            "profile_repository": "excluded",
            "archived_repositories": "included",
            "commits": "GitHub-attributed author; default branch; non-merge; unique SHA; author date within window",
            "language_measure": "GitHub language bytes, not time or proficiency",
        },
        "repositories": source_repositories,
        "languages": {"total_bytes": sum(row["bytes"] for row in languages), "items": languages},
        "activity": aggregate_activity(commits, owner, start, now),
    }


LABELS = {
    "en": {
        "languages": "Languages in my public repositories",
        "bytes": "Share of language bytes",
        "language_note": "Code composition, not proficiency or time spent.",
        "activity": "When I commit",
        "activity_scope": "Non-merge commits on default branches",
        "activity_note": "Author timestamps in Japan time. Commit activity is not working time.",
        "repositories": "public owned repositories · forks and profile excluded",
        "commits": "commits",
        "days": "active days",
        "hours": "Hour of day · JST (UTC+09:00)",
        "no_languages": "No language byte data available.",
        "no_activity": "No matching public commits in this period.",
        "updated": "Updated",
    },
}


def text(x, y, value, size=16, color="text", weight=400, anchor="start"):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{COLORS[color]}" font-weight="{weight}" text-anchor="{anchor}">{escape(str(value))}</text>'


def svg_document(title: str, description: str, height: int, body: list[str], width: int = 840) -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
        f'<title id="title">{escape(title)}</title>',
        f'<desc id="description">{escape(description)}</desc>',
        f'<rect width="{width}" height="{height}" rx="12" fill="{COLORS["background"]}"/>',
        '<g font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Noto Sans, Noto Sans JP, Arial, sans-serif">',
        *body, "</g>", "</svg>", "",
    ])


def number(value: float) -> str:
    if 0 < value < 0.1:
        return "<0.1%"
    return f"{value:.1f}%"


def render_languages(data: dict, locale: str) -> str:
    labels = LABELS[locale]
    rows = data["languages"]["items"]
    count = len(data["repositories"])
    row_height = 38
    height = 166 + max(1, len(rows)) * row_height
    body = [
        text(30, 40, labels["languages"], 24, weight=600),
        text(30, 68, labels["bytes"], 15, "muted"),
        text(30, 91, f'{count} {labels["repositories"]}', 13, "muted"),
    ]
    if not rows:
        body.append(text(30, 133, labels["no_languages"], 16, "muted"))
    for index, row in enumerate(rows):
        baseline = 127 + index * row_height
        value = row["share_percent"]
        body.extend([
            text(30, baseline, row["name"], 16),
            f'<rect x="230" y="{baseline - 14}" width="480" height="18" rx="3" fill="{COLORS["track"]}"/>',
            f'<rect x="230" y="{baseline - 14}" width="{480 * value / 100:.4f}" height="18" rx="3" fill="{COLORS["bar"]}"/>',
            text(806, baseline, number(value), 16, anchor="end"),
        ])
    update = parse_time(data["generated_at"]).astimezone(JST).strftime("%Y-%m-%d")
    body.extend([
        text(30, height - 38, labels["language_note"], 13, "muted"),
        text(30, height - 17, f'{labels["updated"]} {update} · GitHub REST API', 12, "muted"),
    ])
    accessible = "; ".join(f'{row["name"]}: {row["bytes"]} bytes ({row["share_percent"]:.3f}%)' for row in rows)
    return svg_document(labels["languages"], accessible or labels["no_languages"], height, body)


def nice_maximum(value: int) -> int:
    if value <= 4:
        return 4
    magnitude = 10 ** math.floor(math.log10(value))
    for multiplier in (1, 2, 4, 5, 10):
        candidate = multiplier * magnitude
        if candidate >= value:
            return candidate
    raise AssertionError("Unreachable axis bound")


def render_activity(data: dict, locale: str) -> str:
    labels = LABELS[locale]
    activity = data["activity"]
    hours = activity["hourly_commits_jst"]
    left, top, plot_height, step = 62, 174, 150, 31
    baseline = top + plot_height
    maximum = nice_maximum(max(hours))
    start = parse_time(data["window"]["from"]).astimezone(JST).strftime("%Y-%m-%d")
    end = parse_time(data["window"]["to"]).astimezone(JST).strftime("%Y-%m-%d")
    body = [
        text(30, 40, labels["activity"], 24, weight=600),
        text(30, 68, f'{start} – {end} · {data["window"]["days"]} days', 15, "muted"),
        text(30, 105, f'{activity["commits"]:,} {labels["commits"]}   /   {activity["active_days"]} {labels["days"]}', 20, weight=600),
        text(30, 133, labels["activity_scope"], 13, "muted"),
        text(left, top - 16, labels["commits"], 12, "muted"),
    ]
    ticks = sorted(set([0, maximum // 2, maximum]))
    for tick in ticks:
        y = baseline - plot_height * tick / maximum
        body.extend([
            f'<line x1="{left}" y1="{y}" x2="806" y2="{y}" stroke="{COLORS["grid"]}"/>',
            text(left - 10, y + 4, f"{tick:,}", 12, "muted", anchor="end"),
        ])
    for hour, count in enumerate(hours):
        x = left + hour * step
        bar_height = plot_height * count / maximum
        body.append(f'<rect x="{x + 4}" y="{baseline - bar_height:.4f}" width="23" height="{bar_height:.4f}" rx="2" fill="{COLORS["bar"]}"><title>{hour:02d}:00–{hour:02d}:59 JST: {count} {escape(labels["commits"])}</title></rect>')
        if hour % 3 == 0 or hour == 23:
            body.append(text(x + 15.5, baseline + 23, f"{hour:02d}", 13, "muted", anchor="middle"))
    if not activity["commits"]:
        body.append(text(434, top + plot_height / 2, labels["no_activity"], 17, "muted", anchor="middle"))
    body.extend([
        text(434, baseline + 51, labels["hours"], 14, "muted", anchor="middle"),
        text(30, 410, labels["activity_note"], 13, "muted"),
        text(30, 434, f'{len(data["repositories"])} {labels["repositories"]}', 12, "muted"),
        text(30, 455, f'{labels["updated"]} {end} · GitHub REST API', 12, "muted"),
    ])
    description = f'{start}–{end}, JST; ' + "; ".join(f'{hour:02d}: {count}' for hour, count in enumerate(hours))
    return svg_document(labels["activity"], description, 477, body)


COMPACT_LABELS = {
    "en": {
        "languages": "Public repository languages",
        "scope": "Public owned repositories",
        "excluded": "Forks and profile excluded",
        "bytes": "Share of language bytes",
        "language_note": ["Code share, not proficiency or time spent."],
        "activity_scope": ["Default branches · non-merge commits"],
        "activity_note": ["Author timestamps in Japan time.", "Commit activity is not working time."],
    },
}


def render_languages_compact(data: dict, locale: str) -> str:
    labels = LABELS[locale]
    compact = COMPACT_LABELS[locale]
    rows = data["languages"]["items"]
    first_row = 146
    row_height = 48
    footer_top = first_row + max(1, len(rows)) * row_height + 8
    height = footer_top + len(compact["language_note"]) * 22 + 43
    body = [
        text(22, 36, compact["languages"], 20, weight=600),
        text(22, 64, compact["bytes"], 14, "muted"),
        text(22, 88, f'{compact["scope"]}: {len(data["repositories"])}', 14, "muted"),
        text(22, 110, compact["excluded"], 14, "muted"),
    ]
    if not rows:
        body.append(text(22, first_row, labels["no_languages"], 16, "muted"))
    for index, row in enumerate(rows):
        baseline = first_row + index * row_height
        value = row["share_percent"]
        body.extend([
            text(22, baseline, row["name"], 16),
            text(398, baseline, number(value), 16, anchor="end"),
            f'<rect x="22" y="{baseline + 10}" width="376" height="10" rx="3" fill="{COLORS["track"]}"/>',
            f'<rect x="22" y="{baseline + 10}" width="{376 * value / 100:.4f}" height="10" rx="3" fill="{COLORS["bar"]}"/>',
        ])
    for index, note in enumerate(compact["language_note"]):
        body.append(text(22, footer_top + index * 22, note, 14, "muted"))
    update = parse_time(data["generated_at"]).astimezone(JST).strftime("%Y-%m-%d")
    body.append(text(22, height - 20, f'{labels["updated"]} {update} · GitHub REST API', 14, "muted"))
    description = "; ".join(f'{row["name"]}: {row["bytes"]} bytes ({row["share_percent"]:.3f}%)' for row in rows)
    return svg_document(compact["languages"], description or labels["no_languages"], height, body, 420)


def render_activity_compact(data: dict, locale: str) -> str:
    labels = LABELS[locale]
    compact = COMPACT_LABELS[locale]
    activity = data["activity"]
    hours = activity["hourly_commits_jst"]
    left, top, plot_height = 46, 176, 136
    step = 352 / 24
    baseline = top + plot_height
    maximum = nice_maximum(max(hours))
    start = parse_time(data["window"]["from"]).astimezone(JST).strftime("%Y-%m-%d")
    end = parse_time(data["window"]["to"]).astimezone(JST).strftime("%Y-%m-%d")
    unit = "days"
    body = [
        text(22, 36, labels["activity"], 20, weight=600),
        text(22, 64, f'{start} – {end}', 14, "muted"),
        text(22, 88, f'{data["window"]["days"]} {unit} · {activity["commits"]:,} {labels["commits"]} · {activity["active_days"]} {labels["days"]}', 16, weight=600),
        text(22, 116, compact["activity_scope"][0], 14, "muted"),
        text(left, top - 18, labels["commits"], 14, "muted"),
    ]
    for tick in sorted(set([0, maximum // 2, maximum])):
        y = baseline - plot_height * tick / maximum
        body.extend([
            f'<line x1="{left}" y1="{y}" x2="398" y2="{y}" stroke="{COLORS["grid"]}"/>',
            text(left - 9, y + 5, f"{tick:,}", 14, "muted", anchor="end"),
        ])
    for hour, count in enumerate(hours):
        x = left + hour * step
        bar_height = plot_height * count / maximum
        body.append(f'<rect x="{x + 2:.4f}" y="{baseline - bar_height:.4f}" width="{step - 4:.4f}" height="{bar_height:.4f}" rx="1" fill="{COLORS["bar"]}"><title>{hour:02d}:00–{hour:02d}:59 JST: {count} {escape(labels["commits"])}</title></rect>')
        if hour in (0, 6, 12, 18, 23):
            body.append(text(f"{x + step / 2:.4f}", baseline + 25, f"{hour:02d}", 14, "muted", anchor="middle"))
    if not activity["commits"]:
        empty_lines = ["No matching public commits", "in this period."]
        for index, line in enumerate(empty_lines):
            body.append(text(222, top + 56 + index * 23, line, 16, "muted", anchor="middle"))
    body.append(text(222, baseline + 52, labels["hours"], 14, "muted", anchor="middle"))
    for index, note in enumerate(compact["activity_note"]):
        body.append(text(22, 401 + index * 22, note, 14, "muted"))
    body.extend([
        text(22, 455, f'{compact["scope"]}: {len(data["repositories"])}', 14, "muted"),
        text(22, 477, compact["excluded"], 14, "muted"),
        text(22, 509, f'{labels["updated"]} {end} · GitHub REST API', 14, "muted"),
    ])
    description = f'{start}–{end}, JST; ' + "; ".join(f'{hour:02d}: {count}' for hour, count in enumerate(hours))
    return svg_document(labels["activity"], description, 531, body, 420)


def output_files(data: dict) -> dict[str, str]:
    files = {"data.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n"}
    for locale in ("en",):
        files[f"languages-{locale}.svg"] = render_languages(data, locale)
        files[f"activity-{locale}.svg"] = render_activity(data, locale)
        files[f"languages-{locale}-compact.svg"] = render_languages_compact(data, locale)
        files[f"activity-{locale}-compact.svg"] = render_activity_compact(data, locale)
    return files


def write_outputs(files: dict[str, str], destination: Path):
    """Stage all bytes before replacing published files; API failures write nothing."""
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stats-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        for name, contents in files.items():
            (staging / name).write_text(contents, encoding="utf-8", newline="\n")
        for name in files:
            os.replace(staging / name, destination / name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="31916")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets/stats")
    parser.add_argument("--now", help="Optional ISO 8601 timestamp for reproducible generation")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", arguments.owner):
        parser.error("Invalid GitHub owner")
    now = parse_time(arguments.now) if arguments.now else datetime.now(UTC)
    try:
        data = collect(GitHubApi(), arguments.owner, now)
        files = output_files(data)
        write_outputs(files, arguments.output_dir)
    except (ApiError, ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Statistics refresh failed: {error}", file=sys.stderr)
        return 1
    print(f'Updated statistics for {len(data["repositories"])} public repositories and {data["activity"]["commits"]} commits.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

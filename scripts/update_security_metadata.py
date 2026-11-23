#!/usr/bin/env python3
"""Refresh metadata for the security entries this fork adds.

Upstream's daily bot only runs in awesome-foss/awesome-sysadmin-data, so the
entries we keep locally never get refreshed. This script does the same job for
just those files: stars, last-activity date, archived flag and, when the repo
publishes one, the latest release.

Run locally with a token, or let .github/workflows/update-security-metadata.yml
run it daily in the fork.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ROOT / "scripts" / "security-entries.txt"
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def api(path: str) -> dict | None:
    headers = {
        "User-Agent": "awesome-sysadmin-security-metadata/1.0",
        "Accept": "application/vnd.github+json",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def repo_slug(source_code_url: str) -> str:
    parts = source_code_url.rstrip("/").removesuffix(".git").split("/")
    return "/".join(parts[-2:])


def replace_block(text: str, name: str, body: str) -> str:
    pattern = rf"(?m)^{name}:\n(?:[ \t]+.*\n)+"
    if re.search(pattern, text):
        return re.sub(pattern, body, text, count=1)
    return text


def main() -> int:
    entries = [line.strip() for line in ENTRIES.read_text().splitlines() if line.strip()]
    failures = 0
    for rel in entries:
        path = ROOT / rel
        data = yaml.safe_load(path.read_text()) or {}
        source = data.get("source_code_url", "")
        if "github.com" not in source:
            print(f"skip {rel}: no GitHub source")
            continue
        slug = repo_slug(source)
        try:
            info = api(f"/repos/{slug}")
            if not info:
                print(f"skip {rel}: repo not found")
                failures += 1
                continue
            release = api(f"/repos/{slug}/releases/latest")

            text = path.read_text()
            text, n_stars = re.subn(
                r"(?m)^stargazers_count:\s*\d+\s*$",
                f"stargazers_count: {info['stargazers_count']}",
                text,
            )
            text, n_date = re.subn(
                r"(?m)^updated_at:\s*'?\d{4}-\d{2}-\d{2}'?\s*$",
                f"updated_at: '{(info.get('pushed_at') or '')[:10]}'",
                text,
            )
            text, n_arch = re.subn(
                r"(?m)^archived:\s*(?:true|false)\s*$",
                f"archived: {'true' if info.get('archived') else 'false'}",
                text,
            )
            if release and release.get("tag_name"):
                text = replace_block(
                    text,
                    "current_release",
                    "current_release:\n"
                    f"  tag: {release['tag_name']}\n"
                    f"  published_at: '{(release.get('published_at') or '')[:10]}'\n",
                )
            path.write_text(text)
            print(f"{data.get('name', rel):26} stars={info['stargazers_count']:>6} "
                  f"archived={bool(info.get('archived'))} release={release.get('tag_name') if release else '-'}")
        except Exception as exc:  # network, API, YAML
            print(f"FAIL {rel}: {exc}")
            failures += 1
        time.sleep(0.3)

    if failures:
        print(f"{failures} entr{'y' if failures == 1 else 'ies'} failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

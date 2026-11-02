#!/usr/bin/env python3
"""Rebuild this branch on upstream/master, keeping the security entries.

Designed to run in CI (scripts/sync-upstream.yml) as well as locally. The
branch is an overlay, not a chain of commits to rebase: upstream never owns the
files listed in scripts/security-files.txt, so the reliable operation is to
check out upstream and copy those files on top. That cannot conflict, and it
either moves forward or changes nothing.

Before pushing it restores our files from the newest remote tip, so metadata
the daily workflow wrote while this ran is carried over rather than dropped.
A force-with-lease failure retries once against the newer tip.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "security-files.txt"
UPSTREAM_URL = "https://github.com/awesome-foss/awesome-sysadmin-data.git"
TMP_BRANCH = "sync-upstream-tmp"
WAZUH_TAGS = ("SIEM", "Endpoint Security")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], text=True, check=check,
                          capture_output=True)


def out(*args: str) -> str:
    return git(*args).stdout.strip()


def manifest() -> list[str]:
    files = [line.strip() for line in MANIFEST.read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    if not files:
        raise SystemExit(f"empty manifest: {MANIFEST}")
    return files


def patch_automation() -> None:
    path = ROOT / "tags" / "automation.yml"
    text = path.read_text()
    if not re.search(r"(?m)^name: Automation$", text):
        raise SystemExit("tags/automation.yml no longer says 'name: Automation'")
    path.write_text(re.sub(r"(?m)^name: Automation$", "name: Build Automation", text))


def patch_wazuh() -> None:
    path = ROOT / "software" / "wazuh.yml"
    text = path.read_text()
    for tag in WAZUH_TAGS:
        if re.search(rf"(?m)^\s+- {re.escape(tag)}\s*$", text):
            continue
        text, count = re.subn(r"(?m)^tags:\n", f"tags:\n  - {tag}\n", text, count=1)
        if not count:
            raise SystemExit("software/wazuh.yml has no tags block")
    path.write_text(text)


def rename_automation_refs() -> int:
    changed = 0
    pattern = re.compile(r"(?m)^([ \t]*)- Automation[ \t]*$")
    for path in (ROOT / "software").glob("*.yml"):
        text = path.read_text()
        new = pattern.sub(r"\1- Build Automation", text)
        if new != text:
            path.write_text(new)
            changed += 1
    return changed


def validate() -> None:
    import yaml

    tags = {}
    for path in (ROOT / "tags").glob("*.yml"):
        data = yaml.safe_load(path.read_text()) or {}
        name = data.get("name", path.stem)
        if name in tags:
            raise SystemExit(f"duplicate tag name: {name}")
        tags[name] = path

    missing = []
    for path in (ROOT / "software").glob("*.yml"):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise SystemExit(f"invalid YAML {path.relative_to(ROOT)}: {exc}")
        for tag in data.get("tags", []) or []:
            if tag not in tags:
                missing.append((path.name, tag))

    if missing:
        raise SystemExit(f"{len(missing)} missing tag reference(s), first: {missing[:3]}")

    for rel in manifest():
        if not (ROOT / rel).exists():
            raise SystemExit(f"manifest file missing after overlay: {rel}")

    print(f"validated {len(tags)} tags, {len(list((ROOT / 'software').glob('*.yml')))} software files")


def build(source_ref: str, upstream_sha: str, branch: str, files: list[str]) -> str | None:
    """Apply the overlay and commit on TMP_BRANCH. Returns the new sha, or None."""
    git("checkout", "-B", TMP_BRANCH, upstream_sha)
    git("checkout", source_ref, "--", *files)
    patch_automation()
    patch_wazuh()
    refs = rename_automation_refs()
    validate()

    git("add", "-A")
    if out("write-tree") == out("rev-parse", f"{source_ref}^{{tree}}"):
        print("already current: overlay matches the source tree")
        return None

    print(out("diff", "--cached", "--stat", source_ref))
    message = (
        f"merge: sync the security catalog with upstream {upstream_sha}\n\n"
        f"Rebuilt on {UPSTREAM_URL} and re-applied the entries upstream will not\n"
        f"take ({len(files)} files), including the Build Automation rename ({refs} file(s)).\n"
    )
    git("commit", "-m", message)
    return out("rev-parse", "HEAD")


def restore(branch: str) -> None:
    if out("rev-parse", "--abbrev-ref", "HEAD") == TMP_BRANCH:
        git("reset", "--hard", check=False)
        git("checkout", branch, check=False)
        git("branch", "-D", TMP_BRANCH, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="build and validate, do not commit or push")
    parser.add_argument("--push", action="store_true", help="force-with-lease push the branch")
    args = parser.parse_args()

    if git("status", "--porcelain").stdout.strip():
        raise SystemExit("worktree is dirty; commit or stash first")

    branch = out("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise SystemExit("detached HEAD; check out the security branch first")

    files = manifest()
    git("fetch", UPSTREAM_URL, "master")
    upstream_sha = out("rev-parse", "FETCH_HEAD")
    print(f"upstream: {upstream_sha[:8]}")

    backup = f"backup-{branch}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    git("branch", backup, branch)
    print(f"backup branch: {backup}")

    try:
        git("fetch", "origin", branch)
        source = f"origin/{branch}"
        new_sha = build(source, upstream_sha, branch, files)
        if new_sha is None:
            print("nothing to do")
            return 0

        print(out("log", "-1", "--oneline", new_sha))
        if args.dry_run:
            print("dry run: overlay validated, branch not moved")
            return 0

        git("branch", "-f", branch, new_sha)
        git("checkout", branch)
        git("branch", "-D", TMP_BRANCH, check=False)

        if args.push:
            try:
                git("push", "--force-with-lease", "origin", f"{branch}:{branch}")
                print("pushed to origin (force-with-lease)")
            except subprocess.CalledProcessError:
                print("push raced another writer; rebuilding from the newer tip")
                git("fetch", "origin", branch)
                source = f"origin/{branch}"
                new_sha = build(source, upstream_sha, branch, files)
                if new_sha is None:
                    print("another writer already applied the same overlay")
                    return 0
                git("branch", "-f", branch, new_sha)
                git("checkout", branch)
                git("branch", "-D", TMP_BRANCH, check=False)
                git("push", "--force-with-lease", "origin", f"{branch}:{branch}")
                print("pushed to origin on retry")
        return 0
    finally:
        restore(branch)


if __name__ == "__main__":
    raise SystemExit(main())

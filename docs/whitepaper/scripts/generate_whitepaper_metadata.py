#!/usr/bin/env python3
"""Inject git commit hash and date into whitepaper .qmd/.md files.

Usage:
    python3 scripts/generate_whitepaper_metadata.py
    python3 scripts/generate_whitepaper_metadata.py --restore
"""

import argparse
import subprocess
import sys
from pathlib import Path


def get_git_meta(repo_path: str | Path) -> tuple[str, str]:
    repo = Path(repo_path).resolve()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=repo, timeout=10,
        ).stdout.strip()
        date = subprocess.run(
            ["git", "log", "-1", "--format=%cd", "--date=short"],
            capture_output=True, text=True, cwd=repo, timeout=10,
        ).stdout.strip()
        return commit, date
    except Exception:
        return "unknown", "unknown"


def inject_metadata(file_path: Path, commit: str, date: str) -> bool:
    if not file_path.exists():
        return False
    content = file_path.read_text(encoding="utf-8")
    new_content = content.replace("GIT_COMMIT_PLACEHOLDER", commit)
    new_content = new_content.replace("GIT_DATE_PLACEHOLDER", date)
    if new_content == content:
        return False
    file_path.write_text(new_content, encoding="utf-8")
    print(f"  Injected: {file_path.name}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=None)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()

    # Script lives at docs/whitepaper/scripts/generate_whitepaper_metadata.py
    repo = Path(args.repo) if args.repo else \
        Path(__file__).resolve().parent.parent.parent.parent
    whitepaper_dir = repo / "docs" / "whitepaper"

    if not whitepaper_dir.exists():
        print(f"ERROR: {whitepaper_dir} not found", file=sys.stderr)
        sys.exit(1)

    commit, date = get_git_meta(repo)
    print(f"  Commit: {commit}  Date: {date}")

    ph_commit, ph_date = "GIT_COMMIT_PLACEHOLDER", "GIT_DATE_PLACEHOLDER"
    files = list(whitepaper_dir.glob("*.qmd")) + list(whitepaper_dir.glob("*.md"))
    count = 0

    for f in sorted(files):
        content = f.read_text(encoding="utf-8")
        if args.restore:
            new_content = content.replace(commit, ph_commit).replace(date, ph_date)
        else:
            new_content = content.replace(ph_commit, commit).replace(ph_date, date)
        if new_content != content:
            f.write_text(new_content, encoding="utf-8")
            print(f"  {'Restored' if args.restore else 'Injected'}: {f.name}")
            count += 1

    print(f"Modified {count} files")


if __name__ == "__main__":
    main()

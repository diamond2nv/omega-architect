#!/usr/bin/env python3
"""Inject git commit hash and date into whitepaper .qmd/.md files.

Replaces GIT_COMMIT_PLACEHOLDER and GIT_DATE_PLACEHOLDER in-place.
Run before `quarto render docs/whitepaper/`.

Usage:
    python3 scripts/generate_whitepaper_metadata.py
    # or with custom repo path:
    python3 scripts/generate_whitepaper_metadata.py --repo /path/to/repo
"""

import argparse
import subprocess
import sys
from pathlib import Path


def get_git_meta(repo_path: str | Path) -> tuple[str, str]:
    """Get current commit hash and date from git."""
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
    except subprocess.TimeoutExpired:
        print("WARNING: git timeout, using fallback", file=sys.stderr)
        return "unknown", "unknown"
    except FileNotFoundError:
        print("WARNING: git not found, using fallback", file=sys.stderr)
        return "unknown", "unknown"


def inject_metadata(file_path: Path, commit: str, date: str) -> bool:
    """Replace placeholders in a single file. Returns True if modified."""
    if not file_path.exists():
        return False

    content = file_path.read_text(encoding="utf-8")
    new_content = content.replace("GIT_COMMIT_PLACEHOLDER", commit)
    new_content = new_content.replace("GIT_DATE_PLACEHOLDER", date)

    if new_content == content:
        return False  # no placeholders found

    file_path.write_text(new_content, encoding="utf-8")
    print(f"  Injected: {file_path.name}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Inject git metadata into whitepaper source files"
    )
    parser.add_argument(
        "--repo",
        default=Path(__file__).resolve().parent.parent.parent.parent,
        help="Repository root path (default: parent of scripts/)",
    )
    parser.add_argument(
        "--whitepaper-dir",
        default=None,
        help="Whitepaper directory (default: <repo>/docs/whitepaper)",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore placeholders after render (for git cleanliness)",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    whitepaper_dir = (
        Path(args.whitepaper_dir).resolve()
        if args.whitepaper_dir
        else repo / "docs" / "whitepaper"
    )

    if not whitepaper_dir.exists():
        print(f"ERROR: whitepaper directory not found: {whitepaper_dir}", file=sys.stderr)
        sys.exit(1)

    commit, date = get_git_meta(repo)
    print(f"  Commit: {commit}")
    print(f"  Date:   {date}")
    print(f"  Dir:    {whitepaper_dir}")

    if args.restore:
        # Restore placeholders
        placeholder_commit = "GIT_COMMIT_PLACEHOLDER"
        placeholder_date = "GIT_DATE_PLACEHOLDER"
        count = 0
        for f in whitepaper_dir.glob("*.qmd"):
            content = f.read_text(encoding="utf-8")
            new_content = content.replace(commit, placeholder_commit)
            new_content = new_content.replace(date, placeholder_date)
            if new_content != content:
                f.write_text(new_content, encoding="utf-8")
                print(f"  Restored: {f.name}")
                count += 1
        for f in whitepaper_dir.glob("*.md"):
            if "METADATA" in f.name or "VERSION" in f.name:
                content = f.read_text(encoding="utf-8")
                new_content = content.replace(commit, placeholder_commit)
                new_content = new_content.replace(date, placeholder_date)
                if new_content != content:
                    f.write_text(new_content, encoding="utf-8")
                    print(f"  Restored: {f.name}")
                    count += 1
        print(f"Restored {count} files")
        return

    # Inject
    count = 0
    for f in sorted(whitepaper_dir.glob("*.qmd")):
        if inject_metadata(f, commit, date):
            count += 1
    for f in sorted(whitepaper_dir.glob("*.md")):
        if inject_metadata(f, commit, date):
            count += 1
    print(f"Modified {count} files")


if __name__ == "__main__":
    main()

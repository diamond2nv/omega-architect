#!/usr/bin/env python3
"""Inject git commit hash, date, and repo version into user_guide .qmd/.md files.

Usage:
    python3 scripts/generate_user_guide_metadata.py          # inject
    python3 scripts/generate_user_guide_metadata.py --restore # restore placeholders

Pipeline:
    python3 scripts/generate_user_guide_metadata.py \
        && quarto render \
        && python3 scripts/generate_user_guide_metadata.py --restore
"""

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path


def get_git_meta(repo_path: str | Path) -> tuple[str, str]:
    """Get short commit hash and short date from git."""
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
        return commit or "unknown", date or "unknown"
    except Exception:
        return "unknown", "unknown"


def get_repo_version(repo_path: str | Path) -> str:
    """Read version from pyproject.toml (single source of truth)."""
    pyproject = Path(repo_path) / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def patch_file(file_path: Path, replacements: dict[str, str]) -> bool:
    """Replace placeholders in a file. Returns True if modified."""
    if not file_path.exists():
        return False
    content = file_path.read_text(encoding="utf-8")
    new_content = content
    for old, new in replacements.items():
        new_content = new_content.replace(old, new)
    if new_content == content:
        return False
    file_path.write_text(new_content, encoding="utf-8")
    print(f"  Patched: {file_path.name}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Inject/restore git metadata in user_guide files"
    )
    parser.add_argument("--repo", default=None, help="Repo root path")
    parser.add_argument(
        "--restore", action="store_true",
        help="Restore placeholders (run after quarto render)"
    )
    args = parser.parse_args()

    # Locate repo root (script lives at docs/user_guide/scripts/...)
    repo = Path(args.repo) if args.repo else \
        Path(__file__).resolve().parent.parent.parent.parent
    user_guide_dir = repo / "docs" / "user_guide"

    if not user_guide_dir.exists():
        print(f"ERROR: {user_guide_dir} not found", file=sys.stderr)
        sys.exit(1)

    # --- Get metadata ---
    version = get_repo_version(repo)
    commit, date = get_git_meta(repo)

    if args.restore:
        print(f"  Restoring placeholders (was: v{version} / {commit})")
        replacements = {
            version: "VERSION_PLACEHOLDER",
            commit: "GIT_COMMIT_PLACEHOLDER",
            date: "GIT_DATE_PLACEHOLDER",
        }
    else:
        print(f"  Injecting: v{version} / commit={commit} / date={date}")
        replacements = {
            "VERSION_PLACEHOLDER": version,
            "GIT_COMMIT_PLACEHOLDER": commit,
            "GIT_DATE_PLACEHOLDER": date,
        }

    # --- Generate version-info.tex for PDF footer ---
    tex_path = user_guide_dir / f"_book/{user_guide_dir.name}-version.tex"
    # Actually, Quarto uses \jobname which is the output filename.
    # The tex file needs to be placed where XeTeX can find it.
    # Simpler: generate it at user_guide root and include via path.
    if not args.restore:
        tex_content = (
            f"\\fancyfoot[L]{{\\small Doc version: v{version} "
            f"(commit \\texttt{{{commit}}})}}\n"
            f"\\fancyfoot[R]{{\\small {date}}}\n"
        )
        version_tex = user_guide_dir / "version-info.tex"
        version_tex.write_text(tex_content, encoding="utf-8")
        print(f"  Generated: version-info.tex (v{version}, {commit})")

    # --- Patch all .qmd and .md files ---
    files = list(user_guide_dir.glob("*.qmd")) + list(user_guide_dir.glob("*.md"))
    count = 0
    for f in sorted(files):
        if patch_file(f, replacements):
            count += 1

    print(f"  Modified {count} files")

    # On restore, clean up version-info.tex
    if args.restore:
        version_tex = user_guide_dir / "version-info.tex"
        if version_tex.exists():
            version_tex.unlink()
            print("  Removed: version-info.tex")


if __name__ == "__main__":
    main()

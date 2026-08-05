#!/usr/bin/env python3
import argparse
import os
import subprocess
from pathlib import Path, PurePosixPath


SENSITIVE_NAMES = {".env", ".local.env", ".auth.json"}
SENSITIVE_DIRECTORIES = {"backups"}
SENSITIVE_DIRECTORY_PREFIXES = (
    ".migration-backup-",
    ".password-change-backup-",
)
SENSITIVE_DATABASE_MARKERS = (".db", ".sqlite", ".sqlite3", ".vault")


def is_sensitive_path(path):
    parts = PurePosixPath(path).parts
    if not parts:
        return False

    name = parts[-1]
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(".env.") or name.startswith(".local.env"):
        return True
    if ".auth.json" in name:
        return True
    if any(marker in name for marker in SENSITIVE_DATABASE_MARKERS):
        return True
    return any(
        part in SENSITIVE_DIRECTORIES
        or part.startswith(SENSITIVE_DIRECTORY_PREFIXES)
        for part in parts[:-1]
    )


def git_paths(repo_root, *arguments):
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return {
        os.fsdecode(path)
        for path in result.stdout.split(b"\0")
        if path
    }


def staged_paths(repo_root):
    return git_paths(
        repo_root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
    )


def repository_paths(repo_root):
    tracked = git_paths(repo_root, "ls-files", "-z")
    historical = git_paths(
        repo_root,
        "log",
        "--all",
        "--name-only",
        "--format=",
        "-z",
    )
    return tracked | historical


def sensitive_paths(paths):
    return sorted(path for path in paths if is_sensitive_path(path))


def main():
    parser = argparse.ArgumentParser(
        description="Reject sensitive local-data filenames from Git history."
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Check only files staged for the next commit.",
    )
    arguments = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    paths = staged_paths(repo_root) if arguments.staged else repository_paths(repo_root)
    blocked = sensitive_paths(paths)
    if not blocked:
        return 0

    print("Sensitive local-data files must not be committed:")
    for path in blocked:
        print(f"- {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

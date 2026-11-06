"""Keep a compact archive snapshot and only the latest Actions run."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REPOSITORY = "LOVECHEN/nuphy-firmware"


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def remote_refs():
    refs = {}
    for line in command("git", "ls-remote", "--heads", "--tags", "origin").splitlines():
        sha, ref = line.split()
        if ref == "refs/heads/main" or re.fullmatch(r"refs/tags/official-[a-z0-9-]+", ref):
            refs[ref] = sha
    if "refs/heads/main" not in refs:
        raise ValueError("Archive main branch missing")
    return refs


def compact():
    command("git", "add", "--", "README.md")
    old = command("git", "rev-parse", "HEAD")
    refs = remote_refs()
    if refs["refs/heads/main"] != old:
        raise ValueError("Remote changed; refusing to overwrite concurrent edits")
    unchanged = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode
    if unchanged not in (0, 1):
        raise ValueError("Cannot compare staged index")
    age = time.time() - int(command("git", "log", "-1", "--format=%ct"))
    one_root = command("git", "rev-list", "--count", "HEAD") == "1" and all(sha == old for sha in refs.values())
    if unchanged == 0 and one_root and age < 50 * 86400:
        print("Archive snapshot unchanged")
        return
    tree = command("git", "write-tree")
    new = command("git", "commit-tree", tree, "-m", "chore: publish official firmware archive")
    leases = [f"--force-with-lease={ref}:{sha}" for ref, sha in refs.items()]
    updates = [f"{new}:{ref}" for ref in refs]
    command("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "push", "--atomic", *leases, "origin", *updates)
    command("git", "update-ref", "HEAD", new, old)
    for ref in refs:
        if ref.startswith("refs/tags/"):
            command("git", "update-ref", ref, new)
    command("git", "update-ref", "refs/remotes/origin/main", new)
    print("Published one-commit archive snapshot")


def obsolete_runs(pages, current):
    return sorted({row["id"] for page in pages for row in page["workflow_runs"]
                   if row["id"] < current and row["status"] == "completed"})


def prune():
    current = int(os.environ["GITHUB_RUN_ID"])
    pages = json.loads(command("gh", "api", "--paginate", "--slurp",
                               f"repos/{REPOSITORY}/actions/runs?per_page=100"))
    ids = obsolete_runs(pages, current)
    for identity in ids:
        command("gh", "api", "--method", "DELETE", f"repos/{REPOSITORY}/actions/runs/{identity}")
    print(f"Removed {len(ids)} older completed Actions runs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("compact", "prune"))
    args = parser.parse_args()
    try:
        if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
            raise ValueError("Unexpected maintenance repository")
        os.chdir(Path(__file__).resolve().parents[2])
        (compact if args.operation == "compact" else prune)()
    except Exception as error:
        print("Maintenance failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)

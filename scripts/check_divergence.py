#!/usr/bin/env python3
"""
Checks each docs repo's shared Docusaurus directory against the
ML-Shared-Docusaurus source of truth and reports divergences.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOURCE_REPO = "MystenLabs/ML-Shared-Docusaurus"
SOURCE_BRANCH = "master"
SOURCE_PATH = ""  # Subdirectory within source repo to compare (empty = repo root)

TARGETS = [
    {"repo": "MystenLabs/sui", "shared_path": "docs/site/src/shared"},
    {"repo": "MystenLabs/walrus", "shared_path": "docs/site/src/shared"},
    {"repo": "MystenLabs/seal", "shared_path": "docs/site/src/shared"},
    {"repo": "MystenLabs/suins-contracts", "shared_path": "documentation/site/src/shared"},
]

# Top-level files/dirs in the source repo to exclude from comparison
# (repo scaffolding that wouldn't be copied into target shared dirs)
IGNORE = {
    ".git", ".github", ".gitignore", "README.md", "LICENSE", "LICENSE.md",
    "CHANGELOG.md", "node_modules", ".DS_Store", "package.json",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
}

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/shared-sync")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def get_default_branch(repo):
    """Use gh CLI to detect the default branch, fallback to 'main'."""
    r = run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return "main"


def clone_shallow(repo, branch, dest):
    r = run(["git", "clone", "--depth=1", "--branch", branch,
             f"https://github.com/{repo}.git", dest])
    return r.returncode == 0


def clone_sparse(repo, branch, sparse_path, dest):
    """Shallow clone with sparse checkout — only fetches blobs under sparse_path."""
    r = run(["git", "clone", "--depth=1", "--filter=blob:none", "--sparse",
             "--branch", branch, f"https://github.com/{repo}.git", dest])
    if r.returncode != 0:
        return False
    r = run(["git", "sparse-checkout", "set", sparse_path], cwd=dest)
    return r.returncode == 0


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def should_ignore(rel_path):
    parts = Path(rel_path).parts
    return parts[0] in IGNORE if parts else True


def get_file_map(directory):
    """Return {relative_path: sha256} for all non-ignored files."""
    files = {}
    root = Path(directory)
    if not root.is_dir():
        return files
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root))
            if not should_ignore(rel):
                files[rel] = file_hash(p)
    return files


def compare(source_files, target_files):
    """Compare source to target. Only flags files from the source of truth
    that are missing or modified in the target. Repo-specific extras in the
    target are ignored."""
    src = set(source_files)
    tgt = set(target_files)
    return {
        "missing": sorted(src - tgt),
        "modified": sorted(
            k for k in src & tgt if source_files[k] != target_files[k]
        ),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== Shared Docusaurus Sync Check ===\n")

    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR)
    os.makedirs(WORK_DIR)

    # Clone source of truth
    source_dir = os.path.join(WORK_DIR, "source")
    print(f"Cloning source: {SOURCE_REPO} ({SOURCE_BRANCH})")
    if not clone_shallow(SOURCE_REPO, SOURCE_BRANCH, source_dir):
        print("ERROR: Failed to clone source repo")
        sys.exit(1)

    source_root = (
        os.path.join(source_dir, SOURCE_PATH) if SOURCE_PATH else source_dir
    )
    source_files = get_file_map(source_root)
    print(f"Source: {len(source_files)} file(s)\n")

    # Check each target repo
    has_divergence = False
    report = [
        "## Shared Docusaurus Sync Report\n",
        f"Source of truth: `{SOURCE_REPO}` (`{SOURCE_BRANCH}`)\n",
    ]

    for target in TARGETS:
        repo = target["repo"]
        shared_path = target["shared_path"]
        print(f"Checking {repo} ({shared_path})...")

        dest = os.path.join(WORK_DIR, repo.replace("/", "-"))
        branch = get_default_branch(repo)

        if not clone_sparse(repo, branch, shared_path, dest):
            print(f"  Failed to clone\n")
            report.append(f"### {repo}\n**Clone failed**\n")
            continue

        target_root = os.path.join(dest, shared_path)
        if not os.path.isdir(target_root):
            print(f"  {shared_path} not found\n")
            report.append(f"### {repo}\n`{shared_path}` not found in repo\n")
            continue

        target_files = get_file_map(target_root)
        print(f"  {len(target_files)} file(s)")

        diff = compare(source_files, target_files)
        total = len(diff["missing"]) + len(diff["modified"])

        if total == 0:
            print(f"  In sync\n")
            report.append(f"### {repo} — In sync\n")
        else:
            has_divergence = True
            print(f"  {total} divergence(s)")
            report.append(f"### {repo} — {total} divergence(s)\n")
            for f in diff["missing"]:
                print(f"    MISSING:  {f}")
                report.append(f"- **Missing** (in source, not in repo): `{f}`")
            for f in diff["modified"]:
                print(f"    MODIFIED: {f}")
                report.append(f"- **Modified**: `{f}`")
            report.append("")
            print()

    # Write markdown report
    report_text = "\n".join(report)
    report_path = os.path.join(WORK_DIR, "report.md")
    with open(report_path, "w") as f:
        f.write(report_text)
    print(f"Report written to {report_path}")

    # GitHub Actions step summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(report_text)

    if has_divergence:
        print("\nDivergences found.")
        sys.exit(1)
    else:
        print("\nAll repos in sync.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Syncs shared Docusaurus components between ML-Shared-Docusaurus (source of
truth) and each docs repo.

Phase 1 — For files where a target repo has a newer version than the source,
          creates a PR to ML-Shared-Docusaurus with those updates.
Phase 2 — For each target repo, creates a PR pulling the canonical source
          (source + Phase 1 updates) so every repo ends up identical.
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
SOURCE_PATH = ""  # Subdirectory within source repo to compare (empty = root)

TARGETS = [
    {"repo": "MystenLabs/sui", "shared_path": "docs/site/src/shared"},
    {"repo": "MystenLabs/walrus", "shared_path": "docs/site/src/shared"},
    {"repo": "MystenLabs/seal", "shared_path": "docs/site/src/shared"},
    {"repo": "MystenLabs/suins-contracts", "shared_path": "documentation/site/src/shared"},
]

IGNORE = {
    ".git", ".github", ".gitignore", "README.md", "LICENSE", "LICENSE.md",
    "CHANGELOG.md", "node_modules", ".DS_Store", "package.json",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
}

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/shared-sync")
SYNC_BRANCH = "auto-sync/shared-docusaurus"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def gh(args):
    r = run(["gh"] + args)
    if r.returncode != 0 and r.stderr.strip():
        print(f"    gh error: {r.stderr.strip()}")
    return r


def get_default_branch(repo):
    r = gh(["api", f"repos/{repo}", "--jq", ".default_branch"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "main"


def get_commit_date(clone_dir, path):
    """Last commit date for a file using git log on the cloned repo."""
    r = run(["git", "-C", clone_dir, "log", "-1", "--format=%aI", "--", path])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def clone_full(repo, branch, dest):
    """Full clone (needed for creating branches and pushing)."""
    r = run(["git", "clone", "--branch", branch,
             f"https://github.com/{repo}.git", dest])
    return r.returncode == 0


def clone_sparse(repo, branch, sparse_path, dest):
    """Partial clone with sparse checkout (full history, blobs on demand)."""
    r = run(["git", "clone", "--filter=blob:none", "--sparse",
             "--branch", branch, f"https://github.com/{repo}.git", dest])
    if r.returncode != 0:
        return False
    r = run(["git", "sparse-checkout", "set", sparse_path], cwd=dest)
    return r.returncode == 0


def set_push_url(repo, dest):
    """Configure the remote URL with the GH_TOKEN for pushing."""
    token = os.environ.get("GH_TOKEN", "")
    if token:
        run(["git", "remote", "set-url", "origin",
             f"https://x-access-token:{token}@github.com/{repo}.git"], cwd=dest)


def git_config(dest):
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=dest)
    run(["git", "config", "user.email",
         "41898282+github-actions[bot]@users.noreply.github.com"], cwd=dest)


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


def pr_exists(repo, branch):
    r = gh(["pr", "list", "--repo", repo, "--head", branch,
            "--state", "open", "--json", "number", "--jq", "length"])
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")


def create_pr(repo, branch, base, title, body):
    if pr_exists(repo, branch):
        print(f"    PR already open for {branch}, push updated it")
        return
    r = gh(["pr", "create", "--repo", repo,
            "--head", branch, "--base", base,
            "--title", title, "--body", body])
    if r.returncode == 0:
        print(f"    PR created: {r.stdout.strip()}")
    else:
        print(f"    Failed to create PR")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== Shared Docusaurus Sync ===\n")

    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR)
    os.makedirs(WORK_DIR)

    # ---- Clone source (full, since we need to push to it) ----
    source_dir = os.path.join(WORK_DIR, "source")
    print(f"Cloning source: {SOURCE_REPO} ({SOURCE_BRANCH})")
    if not clone_full(SOURCE_REPO, SOURCE_BRANCH, source_dir):
        print("ERROR: Failed to clone source")
        sys.exit(1)
    set_push_url(SOURCE_REPO, source_dir)
    git_config(source_dir)

    source_root = os.path.join(source_dir, SOURCE_PATH) if SOURCE_PATH else source_dir
    source_files = get_file_map(source_root)
    print(f"Source: {len(source_files)} file(s)\n")

    # ---- Clone targets ----
    target_info = {}  # repo -> {dest, root, shared_path, branch, files}

    for target in TARGETS:
        repo = target["repo"]
        shared_path = target["shared_path"]
        print(f"Cloning {repo}...")

        dest = os.path.join(WORK_DIR, repo.replace("/", "-"))
        branch = get_default_branch(repo)

        if not clone_sparse(repo, branch, shared_path, dest):
            print(f"  Failed to clone\n")
            continue

        target_root = os.path.join(dest, shared_path)
        if not os.path.isdir(target_root):
            print(f"  {shared_path} not found\n")
            continue

        set_push_url(repo, dest)
        git_config(dest)

        target_files = get_file_map(target_root)
        target_info[repo] = {
            "dest": dest,
            "root": target_root,
            "shared_path": shared_path,
            "branch": branch,
            "files": target_files,
        }
        print(f"  {len(target_files)} file(s)\n")

    # ---- Detect modified files and determine direction ----
    # Collect all files that differ between source and any target
    modified_files = {}  # rel_file -> {repo: target_root_path}
    for repo, info in target_info.items():
        for f in source_files:
            if f in info["files"] and source_files[f] != info["files"][f]:
                modified_files.setdefault(f, {})[repo] = info["root"]

    if not modified_files and not any(
        set(source_files) - set(info["files"]) for info in target_info.values()
    ):
        print("Everything is in sync. Nothing to do.")
        return

    print(f"Checking commit dates for {len(modified_files)} modified file(s)...\n")

    push_to_source = {}  # rel_file -> (abs_path_of_newest, from_repo)

    for f in sorted(modified_files):
        src_git_path = f"{SOURCE_PATH}/{f}" if SOURCE_PATH else f
        src_date = get_commit_date(source_dir, src_git_path)

        newest_repo = None
        newest_date = src_date
        newest_abs = None

        for repo, target_root in modified_files[f].items():
            tgt_git_path = f"{target_info[repo]['shared_path']}/{f}"
            tgt_date = get_commit_date(target_info[repo]["dest"], tgt_git_path)

            if tgt_date and (not newest_date or tgt_date > newest_date):
                newest_date = tgt_date
                newest_repo = repo
                newest_abs = os.path.join(target_root, f)

        if newest_repo:
            push_to_source[f] = (newest_abs, newest_repo)
            short = newest_repo.split("/")[1]
            print(f"  {f}  ->  {short} is newer ({newest_date[:10] if newest_date else '?'})")
        else:
            print(f"  {f}  ->  source is newer ({src_date[:10] if src_date else '?'})")

    # ---- Phase 1: Push newer target files to source ----
    if push_to_source:
        print(f"\n--- Phase 1: pushing {len(push_to_source)} file(s) to source ---\n")

        # Create or reset sync branch
        run(["git", "checkout", SOURCE_BRANCH], cwd=source_dir)
        run(["git", "branch", "-D", SYNC_BRANCH], cwd=source_dir)
        run(["git", "checkout", "-b", SYNC_BRANCH], cwd=source_dir)

        for f, (abs_path, from_repo) in sorted(push_to_source.items()):
            dest_path = os.path.join(source_root, f)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copy2(abs_path, dest_path)
            print(f"  {f}  <-  {from_repo.split('/')[1]}")

        run(["git", "add", "-A"], cwd=source_dir)
        repos_short = sorted(set(r.split("/")[1] for _, (_, r) in push_to_source.items()))
        msg = f"sync: pull newer shared components from {', '.join(repos_short)}"
        commit = run(["git", "commit", "-m", msg], cwd=source_dir)

        if commit.returncode != 0:
            print("  No changes to commit (source already up to date)")
        else:
            push = run(["git", "push", "--force", "origin", SYNC_BRANCH], cwd=source_dir)
            if push.returncode == 0:
                body_lines = ["Pulls newer changes from docs repos:\n"]
                for f, (_, repo) in sorted(push_to_source.items()):
                    body_lines.append(f"- `{f}` from **{repo}**")
                create_pr(SOURCE_REPO, SYNC_BRANCH, SOURCE_BRANCH,
                          "sync: pull newer shared components from docs repos",
                          "\n".join(body_lines))
            else:
                print(f"  Push failed: {push.stderr.strip()}")
    else:
        print("\nSource is already up to date — no Phase 1 PR needed.")

    # ---- Build canonical file set (source + newer target overrides) ----
    canonical_dir = os.path.join(WORK_DIR, "canonical")
    if os.path.exists(canonical_dir):
        shutil.rmtree(canonical_dir)

    # Start from source
    for f, h in source_files.items():
        src = os.path.join(source_root, f)
        dst = os.path.join(canonical_dir, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    # Overlay newer target files
    for f, (abs_path, _) in push_to_source.items():
        dst = os.path.join(canonical_dir, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(abs_path, dst)

    canonical_files = get_file_map(canonical_dir)

    # ---- Phase 2: Sync canonical source to each target ----
    print(f"\n--- Phase 2: syncing canonical source to targets ---\n")

    for target in TARGETS:
        repo = target["repo"]
        if repo not in target_info:
            continue

        info = target_info[repo]
        dest = info["dest"]
        target_root = info["root"]
        base_branch = info["branch"]
        tgt_files = info["files"]

        # Figure out which files need updating
        to_update = []
        for f in canonical_files:
            if f not in tgt_files or canonical_files[f] != tgt_files[f]:
                to_update.append(f)

        if not to_update:
            print(f"  {repo}: already matches canonical — skipping")
            continue

        print(f"  {repo}: {len(to_update)} file(s) to update")

        run(["git", "checkout", base_branch], cwd=dest)
        run(["git", "branch", "-D", SYNC_BRANCH], cwd=dest)
        run(["git", "checkout", "-b", SYNC_BRANCH], cwd=dest)

        for f in sorted(to_update):
            src = os.path.join(canonical_dir, f)
            dst = os.path.join(target_root, f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print(f"    {f}")

        run(["git", "add", "-A"], cwd=dest)
        commit = run(["git", "commit", "-m",
                       "sync: update shared Docusaurus components from source"],
                      cwd=dest)

        if commit.returncode != 0:
            print(f"    No changes to commit")
            continue

        push = run(["git", "push", "--force", "origin", SYNC_BRANCH], cwd=dest)
        if push.returncode == 0:
            body_lines = [
                f"Syncs `{info['shared_path']}` with "
                f"[{SOURCE_REPO}]"
                f"(https://github.com/{SOURCE_REPO}).\n",
                "Updated files:\n",
            ]
            for f in sorted(to_update):
                body_lines.append(f"- `{f}`")
            create_pr(repo, SYNC_BRANCH, base_branch,
                      "sync: update shared Docusaurus components",
                      "\n".join(body_lines))
        else:
            print(f"    Push failed: {push.stderr.strip()}")

    print("\nSync complete.")


if __name__ == "__main__":
    main()

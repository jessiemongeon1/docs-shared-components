#!/usr/bin/env python3
"""
Syncs shared Docusaurus components between ML-Shared-Docusaurus (source of
truth) and each docs repo.

Phase 1 — For files where a target repo has a newer version than the source,
          creates a PR to ML-Shared-Docusaurus with those updates.
Phase 2 — For each target repo, creates a PR pulling the canonical source
          (source + Phase 1 updates) so every repo ends up identical.

Uses a fork-based workflow: pushes branches to your fork of each repo,
then opens PRs from the fork to upstream.
"""

import argparse
import hashlib
import os
import re
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

# License headers per file type and repo.
# JS/TS: Mysten uses /* // ... */ block, Walrus uses bare // lines.
# CSS:   Both use /* ... */ but Mysten has // prefixes inside, Walrus doesn't.
WALRUS_REPOS = {"MystenLabs/walrus"}

# Regex that matches any copyright block at the start of a file:
#   /* ... Copyright ... SPDX ... */   or   // Copyright ... // SPDX ...
_LICENSE_RE = re.compile(
    r"^(?:"
    r"/\*[\s\S]*?Copyright \(c\)[\s\S]*?SPDX-License-Identifier:[^\n]*\n\*/\n?"
    r"|"
    r"//\s*Copyright \(c\)[^\n]+\n//\s*SPDX-License-Identifier:[^\n]+\n?"
    r")"
)


def _header_for(repo, filepath):
    """Return the correct license header string for a repo + file type."""
    is_css = filepath.endswith(".css")
    if repo in WALRUS_REPOS:
        if is_css:
            return "/*\n  Copyright (c) Walrus Foundation\n  SPDX-License-Identifier: Apache-2.0\n*/\n"
        return "// Copyright (c) Walrus Foundation\n// SPDX-License-Identifier: Apache-2.0\n"
    else:
        if is_css:
            return "/*\n// Copyright (c) Mysten Labs, Inc.\n// SPDX-License-Identifier: Apache-2.0\n*/\n"
        return "/*\n// Copyright (c) Mysten Labs, Inc.\n// SPDX-License-Identifier: Apache-2.0\n*/\n"


def replace_license(content, repo, filepath):
    """Replace the license header in content to match the target repo."""
    header = _header_for(repo, filepath)
    if _LICENSE_RE.match(content):
        return _LICENSE_RE.sub(header, content, count=1)
    return header + content


def normalize_to_source_license(content, filepath):
    """Normalize any license header to the Mysten Labs style (for source repo)."""
    return replace_license(content, "MystenLabs/source", filepath)


# String literals that reference repo-specific build aliases — files containing
# these are not safely portable across repos and should be skipped during sync.
_REPO_SPECIFIC_IMPORT_RE = re.compile(r'["\']@(?:generated|docs)/')


def has_repo_specific_paths(filepath):
    """True if the file contains imports from repo-specific aliases."""
    try:
        content = Path(filepath).read_text()
        return bool(_REPO_SPECIFIC_IMPORT_RE.search(content))
    except Exception:
        return False


def _strip_for_compare(content):
    """Strip license header and normalize whitespace for content comparison.
    Ignores license differences and trivial blank-line / trailing-whitespace changes."""
    text = _LICENSE_RE.sub("", content)
    text = text.replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    # Collapse consecutive blank lines
    result = []
    prev_blank = False
    for line in lines:
        if line == "":
            if not prev_blank:
                result.append(line)
            prev_blank = True
        else:
            result.append(line)
            prev_blank = False
    return "\n".join(result).strip()


def files_effectively_equal(path_a, path_b):
    """True if two files have the same content ignoring license headers and whitespace."""
    try:
        a = Path(path_a).read_text()
        b = Path(path_b).read_text()
        return _strip_for_compare(a) == _strip_for_compare(b)
    except Exception:
        return False


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


def get_gh_user():
    """Get the authenticated GitHub username."""
    r = gh(["api", "user", "--jq", ".login"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def ensure_fork(upstream_repo):
    """Fork the repo if not already forked. Returns the fork's full name.
    Falls back to upstream if fork can't be created (user may have direct access)."""
    user = get_gh_user()
    if not user:
        print("    Could not determine GitHub user for fork")
        return None

    repo_name = upstream_repo.split("/")[1]
    fork = f"{user}/{repo_name}"

    # Check if the fork already exists and is accessible
    r = gh(["api", f"repos/{fork}", "--jq", ".full_name"])
    if r.returncode == 0 and r.stdout.strip():
        print(f"    Using existing fork: {r.stdout.strip()}")
        return r.stdout.strip()

    # Try to create the fork
    gh(["repo", "fork", upstream_repo, "--clone=false"])

    # Verify the fork actually exists now (don't trust gh exit code)
    r = gh(["api", f"repos/{fork}", "--jq", ".full_name"])
    if r.returncode == 0 and r.stdout.strip():
        print(f"    Forked to: {r.stdout.strip()}")
        return r.stdout.strip()

    # Fork doesn't exist — fall back to pushing directly to upstream
    print(f"    Fork unavailable, will push directly to {upstream_repo}")
    return upstream_repo


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


def setup_push_remote(fork_repo, upstream_repo, dest):
    """Configure a remote for pushing. Returns the remote name to use.
    If fork == upstream, reconfigures 'origin' with auth. Otherwise adds 'fork'."""
    token = os.environ.get("GH_TOKEN", "")
    if token:
        url = f"https://x-access-token:{token}@github.com/{fork_repo}.git"
    else:
        url = f"https://github.com/{fork_repo}.git"

    if fork_repo == upstream_repo:
        # Direct push to upstream — update origin URL with auth
        run(["git", "remote", "set-url", "origin", url], cwd=dest)
        return "origin"
    else:
        # Push to fork
        run(["git", "remote", "remove", "fork"], cwd=dest)
        run(["git", "remote", "add", "fork", url], cwd=dest)
        return "fork"


def git_config(dest):
    r = gh(["api", "user", "--jq", ".login"])
    user = r.stdout.strip() if r.returncode == 0 else "github-actions[bot]"
    r2 = gh(["api", "user", "--jq", ".email // .login"])
    email = r2.stdout.strip() if r2.returncode == 0 else f"{user}@users.noreply.github.com"
    run(["git", "config", "user.name", user], cwd=dest)
    run(["git", "config", "user.email", f"{user}@users.noreply.github.com"], cwd=dest)


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


def pr_exists(upstream_repo, gh_user, branch):
    head = f"{gh_user}:{branch}" if gh_user else branch
    r = gh(["pr", "list", "--repo", upstream_repo, "--head", head,
            "--state", "open", "--json", "number", "--jq", "length"])
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")


def create_pr(upstream_repo, gh_user, branch, base, title, body):
    head = f"{gh_user}:{branch}" if gh_user else branch
    if pr_exists(upstream_repo, gh_user, branch):
        print(f"    PR already open for {head}, push updated it")
        return
    r = gh(["pr", "create", "--repo", upstream_repo,
            "--head", head, "--base", base,
            "--title", title, "--body", body])
    if r.returncode == 0:
        print(f"    PR created: {r.stdout.strip()}")
    else:
        print(f"    Failed to create PR")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(description="Sync shared Docusaurus components")
    p.add_argument("--target", metavar="REPO",
                   help="Only sync a single target repo (e.g. MystenLabs/sui)")
    p.add_argument("--phase2-only", action="store_true",
                   help="Skip Phase 1 (push to source). Only pull source to targets.")
    return p.parse_args()


def main():
    args = parse_args()

    print("=== Shared Docusaurus Sync ===\n")

    # Detect authenticated user
    gh_user = get_gh_user()
    if not gh_user:
        print("ERROR: Could not detect GitHub user. Set GH_TOKEN or run gh auth login.")
        sys.exit(1)
    print(f"Authenticated as: {gh_user}\n")

    # Filter targets if --target is specified
    targets = TARGETS
    if args.target:
        targets = [t for t in TARGETS if t["repo"] == args.target]
        if not targets:
            print(f"ERROR: Unknown target '{args.target}'. Options:")
            for t in TARGETS:
                print(f"  {t['repo']}")
            sys.exit(1)
        print(f"Target: {args.target}\n")

    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR)
    os.makedirs(WORK_DIR)

    # ---- Clone source (full, since we need to push to it) ----
    source_dir = os.path.join(WORK_DIR, "source")
    print(f"Cloning source: {SOURCE_REPO} ({SOURCE_BRANCH})")
    if not clone_full(SOURCE_REPO, SOURCE_BRANCH, source_dir):
        print("ERROR: Failed to clone source")
        sys.exit(1)
    git_config(source_dir)

    source_root = os.path.join(source_dir, SOURCE_PATH) if SOURCE_PATH else source_dir
    source_files = get_file_map(source_root)
    print(f"Source: {len(source_files)} file(s)\n")

    # ---- Clone targets ----
    target_info = {}  # repo -> {dest, root, shared_path, branch, files}

    for target in targets:
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
    # Skip files that only differ by license header or whitespace.
    modified_files = {}  # rel_file -> {repo: target_root_path}
    for repo, info in target_info.items():
        for f in source_files:
            if f in info["files"] and source_files[f] != info["files"][f]:
                src_path = os.path.join(source_root, f)
                tgt_path = os.path.join(info["root"], f)
                if has_repo_specific_paths(src_path) or has_repo_specific_paths(tgt_path):
                    continue
                if not files_effectively_equal(src_path, tgt_path):
                    modified_files.setdefault(f, {})[repo] = info["root"]

    if not modified_files and not any(
        set(source_files) - set(info["files"]) for info in target_info.values()
    ):
        print("Everything is in sync. Nothing to do.")
        return

    push_to_source = {}  # rel_file -> (abs_path_of_newest, from_repo)

    if args.phase2_only:
        print("Phase 1 skipped (--phase2-only)\n")
    else:
        print(f"Checking commit dates for {len(modified_files)} modified file(s)...\n")

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

    # ---- Phase 1: Push newer target files to source via fork ----
    if push_to_source and not args.phase2_only:
        print(f"\n--- Phase 1: pushing {len(push_to_source)} file(s) to source ---\n")

        source_fork = ensure_fork(SOURCE_REPO)
        if not source_fork:
            print("  ERROR: Could not resolve push target for source repo")
        else:
            remote = setup_push_remote(source_fork, SOURCE_REPO, source_dir)
            is_direct = source_fork == SOURCE_REPO
            pr_head_user = gh_user if not is_direct else None

            run(["git", "checkout", SOURCE_BRANCH], cwd=source_dir)
            run(["git", "branch", "-D", SYNC_BRANCH], cwd=source_dir)
            run(["git", "checkout", "-b", SYNC_BRANCH], cwd=source_dir)

            for f, (abs_path, from_repo) in sorted(push_to_source.items()):
                dest_path = os.path.join(source_root, f)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(abs_path, dest_path)
                # Normalize license to Mysten Labs for source repo
                content = Path(dest_path).read_text()
                Path(dest_path).write_text(normalize_to_source_license(content, f))
                print(f"  {f}  <-  {from_repo.split('/')[1]}")

            run(["git", "add", "-A"], cwd=source_dir)
            repos_short = sorted(set(r.split("/")[1] for _, (_, r) in push_to_source.items()))
            msg = f"sync: pull newer shared components from {', '.join(repos_short)}"
            commit = run(["git", "commit", "-m", msg], cwd=source_dir)

            if commit.returncode != 0:
                print("  No changes to commit (source already up to date)")
            else:
                push = run(["git", "push", "--force", remote, SYNC_BRANCH], cwd=source_dir)
                if push.returncode == 0:
                    body_lines = ["Pulls newer changes from docs repos:\n"]
                    for f, (_, repo) in sorted(push_to_source.items()):
                        body_lines.append(f"- `{f}` from **{repo}**")
                    head = f"{pr_head_user}:{SYNC_BRANCH}" if pr_head_user else SYNC_BRANCH
                    create_pr(SOURCE_REPO, pr_head_user, SYNC_BRANCH, SOURCE_BRANCH,
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

    for f in source_files:
        src = os.path.join(source_root, f)
        dst = os.path.join(canonical_dir, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    for f, (abs_path, _) in push_to_source.items():
        dst = os.path.join(canonical_dir, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(abs_path, dst)

    canonical_files = get_file_map(canonical_dir)

    # ---- Phase 2: Sync canonical source to each target via forks ----
    print(f"\n--- Phase 2: syncing canonical source to targets ---\n")

    for target in targets:
        repo = target["repo"]
        if repo not in target_info:
            continue

        info = target_info[repo]
        dest = info["dest"]
        target_root = info["root"]
        base_branch = info["branch"]
        tgt_files = info["files"]

        # Only update files the target already has — don't add repo-specific
        # files from other repos (e.g. generate-llmstxt is sui-only).
        # Skip files that only differ by license header or whitespace.
        to_update = []
        for f in canonical_files:
            if f in tgt_files and canonical_files[f] != tgt_files[f]:
                can_path = os.path.join(canonical_dir, f)
                tgt_path = os.path.join(target_root, f)
                if has_repo_specific_paths(can_path) or has_repo_specific_paths(tgt_path):
                    continue
                if not files_effectively_equal(can_path, tgt_path):
                    to_update.append(f)

        if not to_update:
            print(f"  {repo}: already matches canonical — skipping")
            continue

        print(f"  {repo}: {len(to_update)} file(s) to update")

        fork = ensure_fork(repo)
        if not fork:
            print(f"    ERROR: Could not resolve push target for {repo}")
            continue

        remote = setup_push_remote(fork, repo, dest)
        is_direct = fork == repo
        pr_head_user = gh_user if not is_direct else None

        run(["git", "checkout", base_branch], cwd=dest)
        run(["git", "branch", "-D", SYNC_BRANCH], cwd=dest)
        run(["git", "checkout", "-b", SYNC_BRANCH], cwd=dest)

        for f in sorted(to_update):
            src = os.path.join(canonical_dir, f)
            dst_path = os.path.join(target_root, f)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src, dst_path)
            # Apply the correct license header for this repo
            content = Path(dst_path).read_text()
            Path(dst_path).write_text(replace_license(content, repo, f))
            print(f"    {f}")

        run(["git", "add", "-A"], cwd=dest)
        commit = run(["git", "commit", "-m",
                       "sync: update shared Docusaurus components from source"],
                      cwd=dest)

        if commit.returncode != 0:
            print(f"    No changes to commit")
            continue

        push = run(["git", "push", "--force", remote, SYNC_BRANCH], cwd=dest)
        if push.returncode == 0:
            body_lines = [
                f"Syncs `{info['shared_path']}` with "
                f"[{SOURCE_REPO}]"
                f"(https://github.com/{SOURCE_REPO}).\n",
                "Updated files:\n",
            ]
            for f in sorted(to_update):
                body_lines.append(f"- `{f}`")
            create_pr(repo, pr_head_user, SYNC_BRANCH, base_branch,
                      "sync: update shared Docusaurus components",
                      "\n".join(body_lines))
        else:
            print(f"    Push failed: {push.stderr.strip()}")

    print("\nSync complete.")


if __name__ == "__main__":
    main()

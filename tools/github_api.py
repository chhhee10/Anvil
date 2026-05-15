"""
GitHub API tools — fetches commit diffs, PR details, file changes.
Works with public repos without auth; use GITHUB_TOKEN for higher rate limits.
"""
from __future__ import annotations
import os
import logging
import httpx
import omium
from typing import Optional, List, Dict, Any
from models.schemas import CodeChange

logger = logging.getLogger("newsroom.tools.github")

GITHUB_API = "https://api.github.com"
TIMEOUT = 20.0


def _headers() -> Dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


@omium.trace("github_commit_diff")
def get_commit_diff(repo: str, sha: str) -> List[CodeChange]:
    """
    Fetch files changed in a commit.
    repo: 'owner/name'
    sha: full or short commit SHA
    """
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        files = data.get("files", [])
        changes = []
        for f in files[:20]:  # cap at 20 files
            patch = f.get("patch", "")
            if patch and len(patch) > 2000:
                patch = patch[:2000] + "\n... [truncated]"
            changes.append(CodeChange(
                filename=f.get("filename", ""),
                status=f.get("status", "modified"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                patch=patch,
            ))
        logger.info("Fetched %d file changes from commit %s", len(changes), sha[:7])
        return changes
    except Exception as e:
        logger.error("GitHub commit diff failed for %s@%s: %s", repo, sha, e)
        return []


@omium.trace("github_pr_details")
def get_pr_details(repo: str, pr_number: int) -> Optional[Dict[str, Any]]:
    """Fetch pull request details."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("GitHub PR fetch failed for %s#%d: %s", repo, pr_number, e)
        return None


@omium.trace("github_pr_files")
def get_pr_files(repo: str, pr_number: int) -> List[CodeChange]:
    """Fetch files changed in a pull request."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            files = resp.json()
        changes = []
        for f in files[:20]:
            patch = f.get("patch", "")
            if patch and len(patch) > 2000:
                patch = patch[:2000] + "\n... [truncated]"
            changes.append(CodeChange(
                filename=f.get("filename", ""),
                status=f.get("status", "modified"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                patch=patch,
            ))
        logger.info("Fetched %d file changes from PR #%d", len(changes), pr_number)
        return changes
    except Exception as e:
        logger.error("GitHub PR files failed for %s#%d: %s", repo, pr_number, e)
        return []


def get_repo_info(repo: str) -> Optional[Dict[str, Any]]:
    """Fetch basic repository metadata."""
    url = f"{GITHUB_API}/repos/{repo}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("GitHub repo info failed for %s: %s", repo, e)
        return None

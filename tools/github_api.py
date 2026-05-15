"""
GitHub API Tools — Autonomous PR Actions for QualityEngine AI
Wraps PyGitHub for all write operations:
- Post review comments (approve/request_changes)
- Merge PR
- Close/reject PR
- Create bug report Issue
- Set commit status (pending/success/failure)
- Read file content for test generation
"""
from __future__ import annotations
import logging
import os
from typing import Optional, List, Dict, Any

import httpx
from github import Github, GithubException

logger = logging.getLogger("qualityengine.github")

GITHUB_API   = "https://api.github.com"
TIMEOUT      = 20.0


# ─── Shared clients ───────────────────────────────────────────────────────────

def _get_github() -> Github:
    token = os.environ.get("GITHUB_TOKEN", "")
    return Github(token) if token else Github()


def _headers() -> Dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ─── Read Operations ──────────────────────────────────────────────────────────

def get_pr_changed_files(repo: str, pr_number: int) -> List[str]:
    """Return list of .py file paths changed in the PR."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers(), params={"per_page": 50})
            resp.raise_for_status()
            files = resp.json()
        py_files = [f["filename"] for f in files if f["filename"].endswith(".py")]
        logger.info("PR #%d changed Python files: %s", pr_number, py_files)
        return py_files
    except Exception as e:
        logger.error("Failed to list PR files: %s", e)
        return []


def fetch_pr_source_files(repo: str, paths: List[str], ref: str = "main") -> Dict[str, str]:
    """
    Fetch raw content of multiple Python files from the repo at a given ref.
    Returns {relative_path: file_content} for files that exist.
    Used to populate the test sandbox so imports work.
    """
    result: Dict[str, str] = {}
    for path in paths:
        content = get_file_content(repo, path, ref)
        if content:
            result[path] = content
            logger.info("Fetched source file: %s (%d chars)", path, len(content))
    return result



def get_pr_diff(repo: str, pr_number: int) -> str:
    """Fetch the full unified diff for a PR as a string."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers={**_headers(), "Accept": "application/vnd.github.diff"})
            resp.raise_for_status()
            diff = resp.text
        logger.info("Fetched diff for PR #%d (%d chars)", pr_number, len(diff))
        return diff[:20000]  # cap at 20k chars for token budget
    except Exception as e:
        logger.error("Failed to fetch PR diff for #%d: %s", pr_number, e)
        return ""


def get_pr_changed_files(repo: str, pr_number: int) -> List[str]:
    """Return paths of files changed in a PR (Python files only)."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
    paths: List[str] = []
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            for item in resp.json():
                filename = item.get("filename", "")
                if filename.endswith(".py") and item.get("status") != "removed":
                    paths.append(filename)
        logger.info("PR #%d changed Python files: %s", pr_number, paths)
    except Exception as e:
        logger.error("Failed to list PR #%d files: %s", pr_number, e)
    return paths


def fetch_pr_source_files(
    repo: str, paths: List[str], ref: str
) -> Dict[str, str]:
    """Fetch file contents from the PR head branch for sandbox test execution."""
    sources: Dict[str, str] = {}
    for path in paths:
        content = get_file_content(repo, path, ref=ref)
        if content:
            sources[path] = content
    return sources


def get_file_content(repo: str, path: str, ref: str = "main") -> str:
    """Fetch raw content of a file from the repo at a given ref."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers(), params={"ref": ref})
            resp.raise_for_status()
            import base64
            content = base64.b64decode(resp.json()["content"]).decode("utf-8")
        return content[:8000]
    except Exception as e:
        logger.error("Failed to fetch file %s@%s: %s", path, ref, e)
        return ""


def get_pr_metadata(repo: str, pr_number: int) -> Dict[str, Any]:
    """Fetch PR title, author, branch, commit SHA."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        return {
            "title":      data.get("title", ""),
            "author":     data.get("user", {}).get("login", ""),
            "branch":     data.get("head", {}).get("ref", ""),
            "commit_sha": data.get("head", {}).get("sha", ""),
            "base":       data.get("base", {}).get("ref", "main"),
            "state":      data.get("state", "open"),
        }
    except Exception as e:
        logger.error("Failed to fetch PR #%d metadata: %s", pr_number, e)
        return {}


# ─── Write Operations (new for QualityEngine) ─────────────────────────────────

def set_commit_status(repo: str, sha: str, state: str, description: str,
                      context: str = "QualityEngine AI") -> bool:
    """
    Set commit status: state = 'pending' | 'success' | 'failure' | 'error'
    Shows up as the little ✅/❌ check on the PR.
    """
    url = f"{GITHUB_API}/repos/{repo}/statuses/{sha}"
    payload = {
        "state":       state,
        "description": description[:139],  # GitHub limit
        "context":     context,
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, headers=_headers(), json=payload)
            resp.raise_for_status()
        logger.info("Set commit status: %s → %s", sha[:7], state)
        return True
    except Exception as e:
        logger.error("Failed to set commit status: %s", e)
        return False


def post_pr_comment(repo: str, pr_number: int, body: str) -> Optional[str]:
    """Post a comment on the PR. Returns the comment URL."""
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, headers=_headers(), json={"body": body})
            resp.raise_for_status()
            comment_url = resp.json().get("html_url", "")
        logger.info("Posted PR comment on #%d: %s", pr_number, comment_url)
        return comment_url
    except Exception as e:
        logger.error("Failed to post PR comment: %s", e)
        return None


def post_pr_review(repo: str, pr_number: int, commit_sha: str,
                   body: str, event: str = "COMMENT") -> Optional[str]:
    """
    Post a formal PR review with APPROVE, REQUEST_CHANGES, or COMMENT event.
    event: 'APPROVE' | 'REQUEST_CHANGES' | 'COMMENT'
    """
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
    payload = {"commit_id": commit_sha, "body": body, "event": event}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, headers=_headers(), json=payload)
            resp.raise_for_status()
            review_url = resp.json().get("html_url", "")
        logger.info("Posted PR review (%s) on #%d", event, pr_number)
        return review_url
    except Exception as e:
        logger.error("Failed to post PR review: %s", e)
        return None


def merge_pr(repo: str, pr_number: int, commit_title: str,
             commit_message: str = "") -> bool:
    """Merge the PR using squash merge."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/merge"
    payload = {
        "commit_title":   commit_title[:72],
        "commit_message": commit_message,
        "merge_method":   "squash",
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.put(url, headers=_headers(), json=payload)
            resp.raise_for_status()
        logger.info("✅ Merged PR #%d in %s", pr_number, repo)
        return True
    except Exception as e:
        logger.error("Failed to merge PR #%d: %s", pr_number, e)
        return False


def close_pr(repo: str, pr_number: int) -> bool:
    """Close (reject) the PR without merging."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.patch(url, headers=_headers(), json={"state": "closed"})
            resp.raise_for_status()
        logger.info("❌ Closed PR #%d in %s", pr_number, repo)
        return True
    except Exception as e:
        logger.error("Failed to close PR #%d: %s", pr_number, e)
        return False


def create_bug_issue(repo: str, title: str, body: str,
                     labels: List[str] = None) -> Optional[str]:
    """Create a GitHub Issue (bug report). Returns the issue URL."""
    url = f"{GITHUB_API}/repos/{repo}/issues"
    payload = {
        "title":  title,
        "body":   body,
        "labels": labels or ["bug", "auto-generated", "qualityengine"],
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, headers=_headers(), json=payload)
            resp.raise_for_status()
            issue_url = resp.json().get("html_url", "")
        logger.info("🐛 Created bug issue: %s", issue_url)
        return issue_url
    except Exception as e:
        logger.error("Failed to create bug issue: %s", e)
        return None

def get_file_sha(repo: str, path: str, branch: str) -> Optional[str]:
    """Get the blob SHA of a file on a specific branch (needed for updating)."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=_headers(), params={"ref": branch})
            resp.raise_for_status()
            return resp.json().get("sha")
    except Exception as e:
        logger.error("Failed to get SHA for %s on %s: %s", path, branch, e)
        return None

def update_file_on_branch(repo: str, branch: str, path: str, content: str, commit_message: str) -> bool:
    """Commit and push an updated file directly to a branch."""
    import base64
    sha = get_file_sha(repo, path, branch)
    if not sha:
        logger.error("Cannot update %s, file not found on branch %s", path, branch)
        return False
        
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    payload = {
        "message": commit_message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "sha": sha,
        "branch": branch
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.put(url, headers=_headers(), json=payload)
            resp.raise_for_status()
        logger.info("✅ Pushed direct commit: %s -> %s", commit_message, branch)
        return True
    except Exception as e:
        logger.error("Failed to push commit for %s: %s", path, e)
        return False

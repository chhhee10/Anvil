#!/usr/bin/env python3
"""Create 3 demo PRs and run QualityEngine pipeline on each."""
from __future__ import annotations

import base64
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

REPO = "chhhee10/qualityengine-test"
API = "https://api.github.com"
BASE = "http://localhost:8000"
RUN_SUFFIX = str(int(time.time()))[-6:]  # unique branch suffix per run

PR_SPECS = [
    {
        "branch": "demo/1-clean-merge",
        "file": "metrics_lib.py",
        "title": "[Demo 1] Clean metrics helpers — expect immediate MERGE",
        "body": "QualityEngine demo: good code, should merge without self-heal.",
        "content": '''"""Small metrics helpers — clean, validated code."""
from __future__ import annotations
from typing import List


def mean(values: List[float]) -> float:
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)


def median(values: List[float]) -> float:
    if not values:
        raise ValueError("values must not be empty")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def normalize(score: float, low: float, high: float) -> float:
    if high <= low:
        raise ValueError("high must be greater than low")
    if not (low <= score <= high):
        raise ValueError("score out of range")
    return (score - low) / (high - low)
''',
        "expect": "MERGE",
    },
    {
        "branch": "demo/2-healable",
        "file": "pricing_lib.py",
        "title": "[Demo 2] Pricing helpers — expect MERGE_WITH_FIX after self-heal",
        "body": "QualityEngine demo: correct code; tests may fail once then heal.",
        "content": '''"""Pricing helpers — amounts are in CENTS (integers)."""
from __future__ import annotations


def cents_to_dollars(cents: int) -> float:
    """Convert integer cents to dollar float."""
    if cents < 0:
        raise ValueError("cents cannot be negative")
    return round(cents / 100.0, 2)


def dollars_to_cents(dollars: float) -> int:
    if dollars < 0:
        raise ValueError("dollars cannot be negative")
    return int(round(dollars * 100))


def apply_tax_cents(subtotal_cents: int, rate_percent: float) -> int:
    if subtotal_cents < 0:
        raise ValueError("subtotal cannot be negative")
    if not (0 <= rate_percent <= 100):
        raise ValueError("rate must be 0-100")
    tax = subtotal_cents * rate_percent / 100.0
    return int(round(tax))
''',
        "expect": "MERGE_WITH_FIX",
    },
    {
        "branch": "demo/3-reject",
        "file": "unsafe_ops.py",
        "title": "[Demo 3] Unsafe operations — expect REJECT",
        "body": "QualityEngine demo: deliberately vulnerable code.",
        "content": '''"""DELIBERATELY INSECURE — demo only."""
import os
import sqlite3

API_SECRET = "sk-live-hardcoded-demo-key-12345"
DB_PATH = "/tmp/demo.db"


def run_user_code(expression: str) -> object:
    return eval(expression)


def run_shell(cmd: str) -> int:
    return os.system(cmd)


def login(username: str, password: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = f"SELECT 1 FROM users WHERE name='{username}' AND pass='{password}'"
    cur.execute(query)
    return cur.fetchone() is not None
''',
        "expect": "REJECT",
    },
]


def headers() -> dict:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def main_sha(client: httpx.Client) -> str:
    r = client.get(f"{API}/repos/{REPO}/git/ref/heads/main", headers=headers())
    r.raise_for_status()
    return r.json()["object"]["sha"]


def create_pr(client: httpx.Client, spec: dict) -> tuple[int, str]:
    sha = main_sha(client)
    branch = f"{spec['branch']}-{RUN_SUFFIX}"
    base, ext = os.path.splitext(spec["file"])
    file_path = f"{base}_{RUN_SUFFIX}{ext}"
    h = headers()

    ref_resp = client.post(
        f"{API}/repos/{REPO}/git/refs",
        headers=h,
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    if ref_resp.status_code == 422:
        # branch exists — reset to main tip
        client.patch(
            f"{API}/repos/{REPO}/git/refs/heads/{branch}",
            headers=h,
            json={"sha": sha, "force": True},
        )

    encoded = base64.b64encode(spec["content"].encode()).decode()
    put_body = {
        "message": spec["title"],
        "content": encoded,
        "branch": branch,
    }
    existing = client.get(
        f"{API}/repos/{REPO}/contents/{file_path}",
        headers=h,
        params={"ref": branch},
    )
    if existing.status_code == 200:
        put_body["sha"] = existing.json()["sha"]
    r = client.put(
        f"{API}/repos/{REPO}/contents/{file_path}",
        headers=h,
        json=put_body,
    )
    r.raise_for_status()

    r = client.post(
        f"{API}/repos/{REPO}/pulls",
        headers=h,
        json={
            "title": spec["title"],
            "head": branch,
            "base": "main",
            "body": spec["body"],
        },
    )
    r.raise_for_status()
    pr = r.json()
    return pr["number"], pr["html_url"]


def trigger(pr_number: int) -> str:
    r = httpx.post(
        f"{BASE}/trigger",
        json={"repo": REPO, "pr_number": pr_number, "topic": f"Demo PR #{pr_number}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["run_id"]


def wait_run(run_id: str, timeout: int = 600) -> dict:
    for _ in range(timeout // 10):
        r = httpx.get(f"{BASE}/status/{run_id}", timeout=30)
        d = r.json()
        if d.get("status") in ("completed", "failed"):
            return d
        time.sleep(10)
    raise TimeoutError(f"Run {run_id} did not finish")


def pr_state(client: httpx.Client, pr_number: int) -> dict:
    r = client.get(f"{API}/repos/{REPO}/pulls/{pr_number}", headers=headers())
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    results = []
    with httpx.Client(timeout=60) as gh:
        for spec in PR_SPECS:
            print(f"\n{'='*60}\nCreating: {spec['expect']}\n{'='*60}")
            pr_num, url = create_pr(gh, spec)
            print(f"PR #{pr_num}: {url}")
            time.sleep(2)
            run_id = trigger(pr_num)
            print(f"Pipeline run: {run_id}")
            outcome = wait_run(run_id)
            verdict = outcome.get("verdict")
            state = pr_state(gh, pr_num)
            row = {
                "pr": pr_num,
                "url": url,
                "expected": spec["expect"],
                "verdict": verdict,
                "merged": state.get("merged"),
                "state": state.get("state"),
                "scores": outcome.get("scores"),
                "heal_steps": [
                    s.get("message", "")[:80]
                    for s in (outcome.get("steps") or [])
                    if s.get("agent") in ("self_healer", "test_generator", "decision_agent")
                ],
            }
            results.append(row)
            ok = verdict == spec["expect"]
            print(f"Verdict: {verdict} (expected {spec['expect']}) {'✅' if ok else '❌'}")
            print(f"GitHub: state={state.get('state')} merged={state.get('merged')}")

    print("\n\n=== SUMMARY ===")
    print(json.dumps(results, indent=2))
    failed = [r for r in results if r["verdict"] != r["expected"]]
    sys.exit(0 if not failed else 1)

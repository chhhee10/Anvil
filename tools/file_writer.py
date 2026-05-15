"""
File writer tool — saves generated reports to disk.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("newsroom.tools.file_writer")

REPORTS_DIR = Path("reports")


def ensure_reports_dir():
    REPORTS_DIR.mkdir(exist_ok=True)


def write_report(run_id: str, topic: str, content: str) -> str:
    """
    Write a markdown report to disk.
    Returns the full file path.
    """
    ensure_reports_dir()
    safe_topic = "".join(c if c.isalnum() or c in "-_ " else "_" for c in topic)
    safe_topic = safe_topic.replace(" ", "_")[:40]
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{safe_topic}_{run_id[:8]}.md"
    filepath = REPORTS_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("Report written to %s", filepath)
    return str(filepath)


def read_report(path: str) -> str:
    """Read a report from disk."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error("Report not found at %s", path)
        return ""

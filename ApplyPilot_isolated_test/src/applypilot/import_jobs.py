"""Import legacy jobs.json data into the ApplyPilot database."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from applypilot.database import get_connection


def import_jobs_json(source: str | Path) -> tuple[int, int]:
    """Import root-level jobs.json records into the ApplyPilot jobs table.

    Returns:
        Tuple of (inserted_count, skipped_duplicates_count).
    """
    path = Path(source).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of jobs")

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    inserted = 0
    skipped = 0

    for row in data:
        apply_url = (row.get("apply_url") or row.get("url") or "").strip()
        if not apply_url:
            skipped += 1
            continue

        title = (row.get("position") or row.get("title") or "").strip() or "Untitled role"
        company = (row.get("company") or row.get("site") or "Imported").strip()
        location = (row.get("location") or "").strip()
        salary = (row.get("salary") or "").strip()
        description = (
            f"Imported from jobs.json | category={row.get('category', '')} "
            f"type={row.get('type', '')} region={row.get('region', '')}"
        ).strip()
        strategy = "import_jobs_json"

        try:
            conn.execute(
                """
                INSERT INTO jobs (
                    url, title, salary, description, location, site, strategy,
                    discovered_at, full_description, application_url, detail_scraped_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    apply_url,
                    title,
                    salary,
                    description,
                    location,
                    company,
                    strategy,
                    now,
                    None,
                    apply_url,
                    None,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1

    conn.commit()
    return inserted, skipped

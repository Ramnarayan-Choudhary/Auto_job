import json

from autojob import database
from autojob.import_jobs import import_jobs_json


def test_import_jobs_json_inserts_and_skips_duplicates(tmp_path, monkeypatch):
    db_path = tmp_path / "autojob.db"
    source = tmp_path / "jobs.json"
    source.write_text(
        json.dumps(
            [
                {
                    "company": "Acme",
                    "position": "Backend Engineer",
                    "location": "Remote",
                    "salary": "$100k",
                    "apply_url": "https://example.com/jobs/1",
                    "category": "Other",
                    "type": "new_grad",
                    "region": "USA",
                },
                {
                    "company": "Acme",
                    "position": "Backend Engineer",
                    "location": "Remote",
                    "salary": "$100k",
                    "apply_url": "https://example.com/jobs/1",
                },
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db(db_path)

    inserted, skipped = import_jobs_json(source)

    assert inserted == 1
    assert skipped == 1

    conn = database.get_connection(db_path)
    row = conn.execute("SELECT title, site, application_url FROM jobs").fetchone()
    assert row["title"] == "Backend Engineer"
    assert row["site"] == "Acme"
    assert row["application_url"] == "https://example.com/jobs/1"

#!/usr/bin/env python3
"""
Job Parser — Extracts all AI/ML job listings from 2026-AI-College-Jobs repo
into a structured jobs.json file.
"""

import json
import re
import os

REPO_DIR = os.path.join(os.path.dirname(__file__), "2026-AI-College-Jobs")
ROOT_DIR = os.path.dirname(__file__)
DASHBOARD_DIR = os.path.join(ROOT_DIR, "dashboard")

FILES_CONFIG = [
    {
        "file": os.path.join(REPO_DIR, "README.md"),
        "type": "internship",
        "region": "USA",
    },
    {
        "file": os.path.join(REPO_DIR, "INTERN_INTL.md"),
        "type": "internship",
        "region": "International",
    },
    {
        "file": os.path.join(REPO_DIR, "NEW_GRAD_USA.md"),
        "type": "new_grad",
        "region": "USA",
    },
    {
        "file": os.path.join(REPO_DIR, "NEW_GRAD_INTL.md"),
        "type": "new_grad",
        "region": "International",
    },
]


def detect_category(line_num, lines):
    """Walk backwards to find the nearest ### heading to determine category."""
    for i in range(line_num, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("### FAANG"):
            return "FAANG+"
        elif stripped.startswith("### Quant"):
            return "Quant"
        elif stripped.startswith("### Other"):
            return "Other"
    return "Other"


def extract_apply_url(cell_html):
    """Extract the first href from an HTML anchor tag in the cell."""
    match = re.search(r'href="([^"]+)"', cell_html)
    return match.group(1) if match else None


def extract_company_name(cell_html):
    """Extract company name from <strong>Company</strong> pattern."""
    match = re.search(r"<strong>([^<]+)</strong>", cell_html)
    return match.group(1) if match else cell_html.strip()


def extract_company_url(cell_html):
    """Extract company website URL."""
    match = re.search(r'href="([^"]+)"', cell_html)
    return match.group(1) if match else None


def parse_table_row(row_text, has_salary=False):
    """Parse a markdown table row into fields."""
    # Split by | and strip
    cells = [c.strip() for c in row_text.split("|")]
    # Remove empty leading/trailing from split
    cells = [c for c in cells if c != ""]

    if has_salary:
        # Format: Company | Position | Location | Salary | Posting | Age
        if len(cells) < 6:
            return None
        return {
            "company_html": cells[0],
            "position": cells[1],
            "location": cells[2],
            "salary": cells[3],
            "posting_html": cells[4],
            "age": cells[5],
        }
    else:
        # Format: Company | Position | Location | Posting | Age
        if len(cells) < 5:
            return None
        return {
            "company_html": cells[0],
            "position": cells[1],
            "location": cells[2],
            "salary": None,
            "posting_html": cells[3],
            "age": cells[4],
        }


def parse_markdown_file(filepath, job_type, region):
    """Parse a single markdown file and return list of job dicts."""
    jobs = []

    if not os.path.exists(filepath):
        print(f"  ⚠ File not found: {filepath}")
        return jobs

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")

    # Determine which sections have salary (FAANG+ and Quant typically do)
    in_table = False
    current_has_salary = False

    for line_num, line in enumerate(lines):
        stripped = line.strip()

        # Detect table header to determine if salary column exists
        if stripped.startswith("| Company") and "Salary" in stripped:
            current_has_salary = True
            in_table = True
            continue
        elif stripped.startswith("| Company"):
            current_has_salary = False
            in_table = True
            continue

        # Skip separator rows
        if stripped.startswith("|---") or stripped.startswith("| ---"):
            continue

        # End of table
        if in_table and (not stripped.startswith("|") or stripped.startswith("<!--")):
            if not stripped.startswith("|"):
                in_table = False
            continue

        # Parse data rows
        if in_table and stripped.startswith("|"):
            row = parse_table_row(stripped, has_salary=current_has_salary)
            if row is None:
                continue

            company_name = extract_company_name(row["company_html"])
            company_url = extract_company_url(row["company_html"])
            apply_url = extract_apply_url(row["posting_html"])
            category = detect_category(line_num, lines)

            # Parse age
            age_str = row["age"].replace("d", "").strip()
            try:
                age_days = int(age_str)
            except ValueError:
                age_days = None

            if apply_url:
                job = {
                    "id": len(jobs) + 1,
                    "company": company_name,
                    "company_url": company_url,
                    "position": row["position"],
                    "location": row["location"],
                    "salary": row["salary"],
                    "apply_url": apply_url,
                    "age_days": age_days,
                    "category": category,
                    "type": job_type,
                    "region": region,
                    "status": "pending",
                }
                jobs.append(job)

    return jobs


def main():
    all_jobs = []
    global_id = 0

    print("🔍 Parsing job listings from 2026-AI-College-Jobs repo...\n")

    for config in FILES_CONFIG:
        filepath = config["file"]
        basename = os.path.basename(filepath)
        jobs = parse_markdown_file(filepath, config["type"], config["region"])

        # Re-assign global IDs
        for job in jobs:
            global_id += 1
            job["id"] = global_id

        all_jobs.extend(jobs)
        print(f"  ✅ {basename}: {len(jobs)} jobs parsed")

    # Save to JSON
    output_path = os.path.join(ROOT_DIR, "jobs.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, indent=2, ensure_ascii=False)

    # Mirror dataset for dashboard when served from ./dashboard directly.
    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    dashboard_jobs_path = os.path.join(DASHBOARD_DIR, "jobs.json")
    with open(dashboard_jobs_path, "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, indent=2, ensure_ascii=False)

    print(f"\n📊 Total jobs parsed: {len(all_jobs)}")
    print(f"💾 Saved to: {output_path}")
    print(f"💾 Dashboard copy: {dashboard_jobs_path}")

    # Print summary
    categories = {}
    types = {}
    regions = {}
    for j in all_jobs:
        categories[j["category"]] = categories.get(j["category"], 0) + 1
        types[j["type"]] = types.get(j["type"], 0) + 1
        regions[j["region"]] = regions.get(j["region"], 0) + 1

    print(f"\n📋 Breakdown:")
    print(f"  By Category: {categories}")
    print(f"  By Type: {types}")
    print(f"  By Region: {regions}")


if __name__ == "__main__":
    main()

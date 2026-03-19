#!/usr/bin/env python3
"""
Auto-Apply Engine — Orchestrates Jobber's AI browser agent
to apply for jobs parsed from 2026-AI-College-Jobs repo.

Usage:
  1. Start Chrome in debug mode (see README.md)
  2. Fill in user_preferences.txt and .env
  3. Run: python3 auto_apply.py --dry-run --limit 10
"""

import asyncio
import json
import os
import sys
import time
import argparse
from datetime import datetime
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(p=None): pass

# Add jobber to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "jobber"))

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

JOBS_FILE = os.path.join(os.path.dirname(__file__), "jobs.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "application_log.json")
PREFS_FILE = os.path.join(os.path.dirname(__file__), "user_preferences.txt")
DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "dashboard")
DASHBOARD_JOBS_FILE = os.path.join(DASHBOARD_DIR, "jobs.json")
DASHBOARD_LOG_FILE = os.path.join(DASHBOARD_DIR, "application_log.json")

# Patterns in agent output that indicate the application FAILED
FAILURE_PATTERNS = [
    "cannot proceed",
    "dom issues",
    "requires authentication",
    "captcha",
    "login required",
    "signup required",
    "login/signup",
    "failed to capture screenshot",
    "error executing",
    "could not be completed",
    "not possible to submit",
    "could not find",
    "unable to",
    "no application form",
    "page not found",
    "404",
    "access denied",
    "forbidden",
    "timed out",
    "connection refused",
    "not accessible",
    "requires an account",
    "sign in",
    "create an account",
]

# Patterns that indicate the application actually SUCCEEDED
SUCCESS_PATTERNS = [
    "application submitted",
    "successfully submitted",
    "application has been submitted",
    "thank you for applying",
    "thanks for applying",
    "application received",
    "successfully applied",
    "your application has been",
    "application complete",
    "submitted your application",
]


def load_jobs(filepath=JOBS_FILE):
    """Load parsed jobs from JSON."""
    with open(filepath, "r") as f:
        return json.load(f)


def _mirror_dashboard_json(source_data, dashboard_path):
    """Mirror JSON payload into dashboard/ so local UI works from that directory."""
    try:
        os.makedirs(DASHBOARD_DIR, exist_ok=True)
        with open(dashboard_path, "w", encoding="utf-8") as f:
            json.dump(source_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠ Warning: could not mirror dashboard data to {dashboard_path}: {e}")


def save_jobs(jobs, filepath=JOBS_FILE):
    """Save jobs back to JSON (with updated statuses)."""
    with open(filepath, "w") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    _mirror_dashboard_json(jobs, DASHBOARD_JOBS_FILE)


def load_log(filepath=LOG_FILE):
    """Load application log (or create empty one)."""
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return json.load(f)
    return {"applied": [], "failed": [], "skipped": []}


def save_log(log, filepath=LOG_FILE):
    """Save application log."""
    with open(filepath, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    _mirror_dashboard_json(log, DASHBOARD_LOG_FILE)


def load_preferences(filepath=PREFS_FILE):
    """Load user preferences text."""
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return f.read()
    return ""


def filter_jobs(jobs, category=None, job_type=None, region=None, max_age_days=None):
    """Filter jobs based on criteria."""
    filtered = jobs
    if category:
        filtered = [j for j in filtered if j["category"] == category]
    if job_type:
        filtered = [j for j in filtered if j["type"] == job_type]
    if region:
        filtered = [j for j in filtered if j["region"] == region]
    if max_age_days:
        filtered = [j for j in filtered if j.get("age_days") and j["age_days"] <= max_age_days]
    return filtered


def get_applied_ids(log):
    """Get set of already-applied job IDs."""
    ids = set()
    for entry in log.get("applied", []):
        ids.add(entry["job_id"])
    for entry in log.get("skipped", []):
        ids.add(entry["job_id"])
    return ids


def classify_result(result_text):
    """
    Classify agent output as success, failure, or uncertain.
    Returns: ('applied', 'failed', or 'uncertain') and a reason string.
    """
    if not result_text:
        return "failed", "No response from agent"

    lower = result_text.lower()

    # Check for explicit success first
    for pattern in SUCCESS_PATTERNS:
        if pattern in lower:
            return "applied", f"Agent confirmed: {pattern}"

    # Check for failure patterns
    for pattern in FAILURE_PATTERNS:
        if pattern in lower:
            return "failed", f"Agent reported issue: {pattern}"

    # If no clear signal, mark as uncertain/failed
def build_apply_command(job, preferences, preferences_dict):
    """Build the natural language command for the browser-use agent."""
    salary_info = f" (Salary: {job['salary']})" if job.get("salary") else ""

    command = (
        f"Navigate to this job application URL: {job['apply_url']}\n\n"
        f"This is a job application for the position of '{job['position']}' at '{job['company']}' "
        f"in {job['location']}{salary_info}.\n\n"
        f"Your task:\n"
        f"1. Go to the URL above\n"
        f"2. Look for the job application form or the 'Apply Now' button on the page. You may need to scroll or wait for it to load.\n"
        f"3. Fill in ALL required fields using my information below.\n"
        f"4. Upload my resume PDF if there is a file upload field. My resume is at {preferences_dict.get('resume_path', 'No path provided')}\n"
        f"5. Submit the application\n"
        f"6. If you encounter a login/signup wall that requires me to create an account or verify email, DO NOT PROCEED. Report 'LOGIN REQUIRED'\n"
        f"7. If you encounter a CAPTCHA, DO NOT PROCEED. Report 'CAPTCHA DETECTED'\n"
        f"8. After successfully submitting the final form and seeing a confirmation page, report 'APPLICATION SUBMITTED SUCCESSFULLY'\n"
        f"9. If submission fails or you cannot complete the form for any reason, report 'APPLICATION FAILED' with the specific reason\n\n"
        f"IMPORTANT: Only say 'APPLICATION SUBMITTED SUCCESSFULLY' if the form was actually "
        f"filled and the submit button was clicked and the page confirmed the submission.\n\n"
        f"My Profile & Preferences:\n{preferences}"
    )
    return command

def parse_preferences(pref_text):
    """Extract dict from preferences text for path injection."""
    prefs = {}
    for line in pref_text.split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            prefs[k.strip().lower()] = v.strip()
    return prefs

async def apply_to_job_with_agent(job, preferences, delay=30):
    """
    Use browser-use Agent to apply to a single job.
    Connects to the user's REAL Chrome browser on port 9222.
    """
    try:
        import os
        from dotenv import load_dotenv
        from browser_use import Agent, Browser
        from langchain_google_genai import ChatGoogleGenerativeAI
        
        # Ensure API keys are loaded
        load_dotenv()
        if not os.environ.get("GOOGLE_API_KEY"):
            return {
                "success": False,
                "status": "failed",
                "result": "GOOGLE_API_KEY not found in environment or .env file",
                "reason": "Missing API Key"
            }

        pref_dict = parse_preferences(preferences)
        command = build_apply_command(job, preferences, pref_dict)

        # Connect browser-use to the existing Chrome instance that the user opened on port 9222
        browser = Browser(
            cdp_url="http://localhost:9222",
            headless=False,
        )

        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.0)
        
        agent = Agent(
            task=command,
            llm=llm,
            browser=browser,
            validate_output=True, # Ensure we get a clear string response 
        )

        print("    🤖 browser-use Agent initialized. Starting task...")
        # Execute the application command
        # The agent.run command requires an asyncio event loop
        # But we are already inside `async def apply_to_job_with_agent`
        # which is launched by asyncio.run() in the main block.
        history = await agent.run()
        
        # The final result is the text of the last message from the agent
        result_str = history.final_result() or "No valid output generated"

        # Properly classify the result
        status, reason = classify_result(result_str)

        print(f"    📋 Agent response: {result_str[:150]}...")
        print(f"    🔍 Classification: {status} — {reason}")

        return {
            "success": status == "applied",
            "status": status,
            "result": result_str,
            "reason": reason,
        }
    except Exception as e:
        return {
            "success": False,
            "status": "failed",
            "result": f"Error: {str(e)}",
            "reason": str(e),
        }


def update_job_status(all_jobs, job_id, new_status):
    """Update the status of a job in the jobs list and save to jobs.json."""
    for job in all_jobs:
        if job["id"] == job_id:
            job["status"] = new_status
            break
    save_jobs(all_jobs)


async def run_batch(jobs, all_jobs, preferences, log, delay=30, dry_run=False):
    """Process a batch of jobs."""
    applied_ids = get_applied_ids(log)
    pending_jobs = [j for j in jobs if j["id"] not in applied_ids]

    total = len(pending_jobs)
    print(f"\n{'='*60}")
    print(f"🚀 Auto-Apply Engine")
    print(f"{'='*60}")
    print(f"📋 Total jobs to process: {total}")
    print(f"⏱  Delay between applications: {delay}s")
    print(f"{'='*60}\n")

    session_applied = 0
    session_failed = 0
    session_uncertain = 0

    for idx, job in enumerate(pending_jobs, 1):
        print(f"\n[{idx}/{total}] 📝 {job['company']} — {job['position']}")
        print(f"    📍 {job['location']}")
        print(f"    🔗 {job['apply_url'][:80]}...")
        print(f"    🏷  {job['category']} | {job['type']} | {job['region']}")

        if dry_run:
            print(f"    ⏭  [DRY RUN] Skipping actual application")
            continue

        timestamp = datetime.now().isoformat()

        try:
            result = await apply_to_job_with_agent(job, preferences, delay)
            status = result.get("status", "failed")

            if status == "applied":
                print(f"    ✅ VERIFIED: Application submitted successfully!")
                session_applied += 1
                log["applied"].append({
                    "job_id": job["id"],
                    "company": job["company"],
                    "position": job["position"],
                    "apply_url": job["apply_url"],
                    "timestamp": timestamp,
                    "result": result["result"],
                    "reason": result.get("reason", ""),
                    "verified": True,
                })
                update_job_status(all_jobs, job["id"], "applied")

            elif status == "uncertain":
                print(f"    ⚠️  UNCERTAIN: Agent finished but couldn't verify submission")
                print(f"    📋 Reason: {result.get('reason', 'Unknown')}")
                session_uncertain += 1
                log["failed"].append({
                    "job_id": job["id"],
                    "company": job["company"],
                    "position": job["position"],
                    "apply_url": job["apply_url"],
                    "timestamp": timestamp,
                    "error": result["result"],
                    "reason": result.get("reason", ""),
                    "status": "uncertain",
                })
                update_job_status(all_jobs, job["id"], "uncertain")

            else:
                print(f"    ❌ FAILED: {result.get('reason', result['result'][:100])}")
                session_failed += 1
                log["failed"].append({
                    "job_id": job["id"],
                    "company": job["company"],
                    "position": job["position"],
                    "apply_url": job["apply_url"],
                    "timestamp": timestamp,
                    "error": result["result"],
                    "reason": result.get("reason", ""),
                    "status": "failed",
                })
                update_job_status(all_jobs, job["id"], "failed")

        except KeyboardInterrupt:
            print(f"\n\n⏹  Interrupted by user. Progress saved.")
            save_log(log)
            sys.exit(0)
        except Exception as e:
            print(f"    ❌ Exception: {str(e)[:100]}")
            session_failed += 1
            log["failed"].append({
                "job_id": job["id"],
                "company": job["company"],
                "position": job["position"],
                "apply_url": job["apply_url"],
                "timestamp": timestamp,
                "error": str(e),
                "reason": str(e),
                "status": "failed",
            })
            update_job_status(all_jobs, job["id"], "failed")

        # Save after each application
        save_log(log)

        # Delay between applications
        if idx < total and not dry_run:
            print(f"    ⏳ Waiting {delay}s before next application...")
            await asyncio.sleep(delay)

    print(f"\n{'='*60}")
    print(f"📊 Session Summary")
    print(f"{'='*60}")
    print(f"  ✅ Verified Applied: {session_applied}")
    print(f"  ⚠️  Uncertain: {session_uncertain}")
    print(f"  ❌ Failed: {session_failed}")
    print(f"{'='*60}")
    print(f"\n📊 All-Time Totals:")
    print(f"  ✅ Applied: {len(log['applied'])}")
    print(f"  ❌ Failed: {len(log['failed'])}")
    print(f"  ⏭  Skipped: {len(log['skipped'])}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Auto-apply to AI/ML jobs")
    parser.add_argument("--category", choices=["FAANG+", "Quant", "Other"],
                        help="Filter by category")
    parser.add_argument("--type", dest="job_type", choices=["internship", "new_grad"],
                        help="Filter by job type")
    parser.add_argument("--region", choices=["USA", "International"],
                        help="Filter by region")
    parser.add_argument("--max-age", type=int, default=None,
                        help="Max age in days (e.g., 30 for jobs posted within last month)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of jobs to process")
    parser.add_argument("--delay", type=int, default=30,
                        help="Delay in seconds between applications (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List jobs without actually applying")
    parser.add_argument("--reset-log", action="store_true",
                        help="Reset application log before starting")
    args = parser.parse_args()

    # Load data
    all_jobs = load_jobs()
    preferences = load_preferences()

    if args.reset_log:
        log = {"applied": [], "failed": [], "skipped": []}
        # Also reset all job statuses
        for job in all_jobs:
            job["status"] = "pending"
        save_jobs(all_jobs)
    else:
        log = load_log()

    # Filter
    filtered = filter_jobs(all_jobs, args.category, args.job_type, args.region, args.max_age)

    if args.limit:
        filtered = filtered[:args.limit]

    if not filtered:
        print("❌ No jobs match your filters. Try different criteria.")
        return

    print(f"\n🔍 Found {len(filtered)} jobs matching your filters")

    # Run
    asyncio.run(run_batch(filtered, all_jobs, preferences, log, args.delay, args.dry_run))


if __name__ == "__main__":
    main()

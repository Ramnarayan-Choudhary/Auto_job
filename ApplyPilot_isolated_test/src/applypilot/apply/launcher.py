"""Apply orchestration using browser-use + Gemini."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    cleanup_on_exit,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
)
from applypilot.apply.dashboard import (
    add_event,
    get_totals,
    init_worker,
    render_full,
    update_state,
)
from applypilot.apply.verification import verify_apply_result
from applypilot.database import get_connection
from applypilot.email import GmailAutomationService

logger = logging.getLogger(__name__)


def _load_blocked():
    from applypilot.config import load_blocked_sites

    return load_blocked_sites()


POLL_INTERVAL = config.DEFAULTS["poll_interval"]
_stop_event = threading.Event()


def _extract_linkedin_job_id(url: str) -> str | None:
    match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)", url)
    if match:
        return match.group(1)
    return None


def _guess_job_title(url: str) -> str:
    lid = _extract_linkedin_job_id(url)
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    # LinkedIn slug URL: /jobs/view/software-engineer-at-microsoft-4381449283
    if "jobs/view/" in path and "-" in path:
        slug = path.split("jobs/view/", 1)[-1]
        slug = re.sub(r"-\d+$", "", slug)
        title = slug.replace("-", " ").strip()
        if title:
            return title.title()
    if lid:
        return f"LinkedIn Job {lid}"
    return "Software Engineer"


def _seed_target_url_job(conn, target_url: str, min_score: int) -> None:
    """Create/update a minimal DB row so --url can run without prior discovery."""
    now = datetime.now(timezone.utc).isoformat()
    parsed = urlparse(target_url)
    host = parsed.netloc.lower()
    site = "linkedin" if "linkedin.com" in host else (host or "direct")
    score = max(min_score, 7)
    resume_txt = os.environ.get("APPLYPILOT_RESUME_TXT", "").strip()
    tailored_path = resume_txt or (str(config.RESUME_PATH) if config.RESUME_PATH.exists() else None)

    conn.execute(
        """
        INSERT INTO jobs (
            url, title, location, site, strategy, discovered_at,
            application_url, fit_score, score_reasoning, scored_at,
            tailored_resume_path, tailor_attempts, apply_attempts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        ON CONFLICT(url) DO UPDATE SET
            application_url = excluded.application_url,
            fit_score = COALESCE(jobs.fit_score, excluded.fit_score),
            tailored_resume_path = COALESCE(jobs.tailored_resume_path, excluded.tailored_resume_path),
            apply_status = CASE WHEN jobs.apply_status = 'in_progress' THEN NULL ELSE jobs.apply_status END
        """,
        (
            target_url,
            _guess_job_title(target_url),
            "",
            site,
            "direct-url",
            now,
            target_url,
            score,
            "Seeded from --url direct target",
            now,
            tailored_path,
        ),
    )


def acquire_job(target_url: str | None = None, min_score: int = 7, worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            normalized = target_url.split("?")[0].rstrip("/")
            like = f"%{normalized}%"
            job_id = _extract_linkedin_job_id(normalized)
            id_like = f"%{job_id}%" if job_id else None
            row = conn.execute(
                """
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (
                      url = ?
                   OR application_url = ?
                   OR application_url LIKE ?
                   OR url LIKE ?
                   OR (? IS NOT NULL AND (url LIKE ? OR application_url LIKE ?))
                )
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
                """,
                (target_url, target_url, like, like, job_id, id_like, id_like),
            ).fetchone()
            if not row:
                _seed_target_url_job(conn, target_url, min_score=min_score)
                row = conn.execute(
                    """
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE url = ?
                      AND (apply_status IS NULL OR apply_status != 'in_progress')
                    LIMIT 1
                    """,
                    (target_url,),
                ).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            params: list = [config.DEFAULTS["max_apply_attempts"], min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)

            row = conn.execute(
                f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
                LIMIT 1
                """,
                params,
            ).fetchone()

        if not row:
            conn.rollback()
            return None

        from applypilot.config import is_manual_ats

        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS', agent_id = NULL WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE jobs
            SET apply_status = 'in_progress',
                agent_id = ?,
                last_attempted_at = ?
            WHERE url = ?
            """,
            (f"worker-{worker_id}", now, row["url"]),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(
    url: str,
    status: str,
    error: str | None = None,
    permanent: bool = False,
    duration_ms: int | None = None,
    task_id: str | None = None,
    verification_confidence: str | None = None,
) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute(
            """
            UPDATE jobs
            SET apply_status = 'applied',
                applied_at = ?,
                apply_error = NULL,
                agent_id = NULL,
                apply_duration_ms = ?,
                apply_task_id = ?,
                verification_confidence = ?
            WHERE url = ?
            """,
            (now, duration_ms, task_id, verification_confidence, url),
        )
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(
            f"""
            UPDATE jobs
            SET apply_status = ?,
                apply_error = ?,
                apply_attempts = {attempts},
                agent_id = NULL,
                apply_duration_ms = ?,
                apply_task_id = ?,
                verification_confidence = ?
            WHERE url = ?
            """,
            (status, error or "unknown", duration_ms, task_id, verification_confidence, url),
        )
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def gen_prompt(target_url: str, min_score: int = 7, model: str = "gemini-2.5-flash", worker_id: int = 0) -> Path | None:
    """Generate the browser-use prompt for manual inspection/debugging."""
    del model  # Prompt contents already include the runtime model choice.
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = txt_path.read_text(encoding="utf-8") if txt_path and txt_path.exists() else ""
    prompt = prompt_mod.build_browser_use_prompt(job=job, tailored_resume=resume_text)
    release_lock(job["url"])

    config.ensure_dirs()
    prompt_file = config.LOG_DIR / f"browser_use_prompt_w{worker_id}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job as applied or failed."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute(
            """
            UPDATE jobs
            SET apply_status = 'applied',
                applied_at = ?,
                apply_error = NULL,
                agent_id = NULL
            WHERE url = ?
            """,
            (now, url),
        )
    else:
        conn.execute(
            """
            UPDATE jobs
            SET apply_status = 'failed',
                apply_error = ?,
                apply_attempts = 99,
                agent_id = NULL
            WHERE url = ?
            """,
            (reason or "manual", url),
        )
    conn.commit()


def reset_failed() -> int:
    """Reset failed or blocked jobs so they can be retried."""
    conn = get_connection()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET apply_status = NULL,
            apply_error = NULL,
            apply_attempts = 0,
            agent_id = NULL,
            confirmation_email_error = NULL
        WHERE apply_status = 'failed'
           OR (apply_status IS NOT NULL AND apply_status != 'applied' AND apply_status != 'in_progress')
        """
    )
    conn.commit()
    return cursor.rowcount


def _read_resume_text(job: dict) -> str:
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    if txt_path and txt_path.exists():
        return txt_path.read_text(encoding="utf-8")
    return ""


def _resolve_resume_pdf_for_upload(job: dict) -> Path | None:
    """Resolve resume PDF path with optional override for better ATS autofill."""
    override = os.environ.get("APPLYPILOT_RESUME_PDF", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        if path.exists():
            return path
        logger.warning("APPLYPILOT_RESUME_PDF not found: %s", path)

    resume_path = job.get("tailored_resume_path")
    if resume_path:
        candidate = Path(resume_path).with_suffix(".pdf")
        if candidate.exists():
            return candidate.resolve()

    if config.RESUME_PDF_PATH.exists():
        return config.RESUME_PDF_PATH.resolve()
    return None


def _load_cover_letter_text(job: dict) -> str | None:
    cover_path = job.get("cover_letter_path")
    if not cover_path:
        return None
    txt_path = Path(cover_path).with_suffix(".txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8")
    if Path(cover_path).suffix == ".txt" and Path(cover_path).exists():
        return Path(cover_path).read_text(encoding="utf-8")
    return None


def _manual_login_timeout_seconds() -> int:
    raw = os.environ.get("APPLYPILOT_MANUAL_LOGIN_TIMEOUT", "50").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 50
    return max(30, min(value, 300))


def _make_email_tools(
    email_service: GmailAutomationService | None,
    worker_id: int,
    headless: bool,
    manual_login_timeout: int = 50,
):
    from browser_use import ActionResult, Tools

    tools = Tools()

    @tools.action("Read recent emails for OTP or verification links")
    async def get_recent_emails(keyword: str = "", max_results: int = 3):
        if email_service is None:
            return ActionResult(error="Gmail automation is not configured")
        emails = email_service.get_recent_emails(keyword=keyword, max_results=max_results)
        if not emails:
            return ActionResult(extracted_content="No recent emails found")

        content = []
        for idx, email in enumerate(emails, 1):
            content.append(
                f"Email {idx}\nFrom: {email['from']}\nSubject: {email['subject']}\nDate: {email['date']}\nBody:\n{email['body']}"
            )
        return ActionResult(
            extracted_content="\n\n".join(content),
            include_extracted_content_only_once=True,
        )

    @tools.action("Get the newest numeric verification code from recent emails")
    async def get_verification_code(keyword: str = ""):
        if email_service is None:
            return ActionResult(error="Gmail automation is not configured")
        code = email_service.extract_verification_code(keyword=keyword)
        if not code:
            return ActionResult(error="No verification code found in recent emails")
        return ActionResult(extracted_content=code, include_extracted_content_only_once=True)

    @tools.action("Send a job application email with an optional attachment")
    async def send_job_email(to: str, subject: str, body: str, attachment_path: str = ""):
        if email_service is None:
            return ActionResult(error="Gmail automation is not configured")
        attachments = [attachment_path] if attachment_path else []
        message_id = email_service.send_email(to=to, subject=subject, body=body, attachments=attachments)
        return ActionResult(extracted_content=f"Email sent with id {message_id}", include_in_memory=True)

    @tools.action("Pause for manual login in the open browser, then continue")
    async def wait_for_manual_login(instruction: str = "", timeout_seconds: int = 50):
        if headless:
            return ActionResult(error="Manual login handoff requires non-headless mode")
        if not sys.stdin.isatty():
            return ActionResult(error="Manual login handoff requires an interactive terminal")

        timeout = max(30, min(timeout_seconds, manual_login_timeout))
        details = instruction.strip() or "Complete login in the browser window."
        add_event(f"[W{worker_id}] Manual login required ({timeout}s): {details[:60]}")

        print(
            f"\n[ApplyPilot][W{worker_id}] Manual login handoff\n"
            f"{details}\n"
            f"Finish login/OTP in Chrome.\n"
            f"Press Enter to resume immediately, or type 'skip' to fail.\n"
            f"Auto-resume in {timeout}s if no terminal input is provided.\n",
            flush=True,
        )

        deadline = time.monotonic() + timeout
        while True:
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                add_event(f"[W{worker_id}] Manual handoff timeout reached, auto-resuming")
                return ActionResult(
                    extracted_content=(
                        "Manual handoff timeout reached. Continue now from the current page, "
                        "complete any remaining required fields, and submit if ready."
                    ),
                    include_in_memory=True,
                )

            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            if not ready:
                continue

            user_input = sys.stdin.readline().strip().lower()
            if user_input in {"skip", "cancel", "abort", "fail"}:
                return ActionResult(error="Manual login was skipped by user")

            add_event(f"[W{worker_id}] Manual login confirmed, resuming automation")
            break

        return ActionResult(
            extracted_content=(
                "Manual login confirmed. Continue immediately: re-check this page, "
                "complete any newly unlocked fields, and proceed to submit."
            ),
            include_in_memory=True,
        )

    return tools


async def _run_browser_agent(
    job: dict,
    port: int,
    model: str,
    dry_run: bool,
    worker_id: int,
    headless: bool,
) -> dict:
    """Run a single browser-use powered application."""
    from browser_use import Agent, ChatGoogle
    from browser_use.browser import BrowserProfile, BrowserSession

    config.load_env()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    profile = config.load_profile()
    automation = profile.get("automation", {})
    email_enabled = config.GMAIL_CREDENTIALS_PATH.exists()
    email_service = GmailAutomationService() if email_enabled else None

    prompt = prompt_mod.build_browser_use_prompt(
        job=job,
        tailored_resume=_read_resume_text(job),
        cover_letter=_load_cover_letter_text(job),
        dry_run=dry_run,
    )

    available_file_paths = []
    resume_pdf = _resolve_resume_pdf_for_upload(job)
    if resume_pdf:
        available_file_paths.append(str(resume_pdf))
    cover_letter_path = job.get("cover_letter_path")
    if cover_letter_path:
        cover_letter_pdf = Path(cover_letter_path).with_suffix(".pdf")
        if cover_letter_pdf.exists():
            available_file_paths.append(str(cover_letter_pdf.resolve()))

    tools = _make_email_tools(
        email_service,
        worker_id=worker_id,
        headless=headless,
        manual_login_timeout=_manual_login_timeout_seconds(),
    )
    browser_session = BrowserSession(
        browser_profile=BrowserProfile(cdp_url=f"http://127.0.0.1:{port}", is_local=True)
    )
    await browser_session.start()

    llm = ChatGoogle(model=model, api_key=api_key, temperature=0.0)
    agent = Agent(
        task=prompt,
        llm=llm,
        browser_session=browser_session,
        tools=tools,
        available_file_paths=available_file_paths,
        max_actions_per_step=2,
        max_failures=2,
        step_timeout=180,
        planning_replan_on_stall=1,
        planning_exploration_limit=1,
        loop_detection_window=8,
        use_judge=False,
    )

    max_steps_env = os.environ.get("APPLYPILOT_AGENT_MAX_STEPS", "").strip()
    try:
        max_steps = int(max_steps_env) if max_steps_env else 70
    except ValueError:
        max_steps = 70
    max_steps = max(40, min(max_steps, 240))

    try:
        history = await agent.run(max_steps=max_steps)
        final_text = history.final_result() or ""
        final_url = ""
        urls = [url for url in history.urls() if url]
        if urls:
            final_url = urls[-1]
        verification = verify_apply_result(final_text=final_text, final_url=final_url)
        return {
            "status": verification.status,
            "reason": verification.reason,
            "verification": verification.verification,
            "confidence": verification.confidence,
            "final_text": final_text,
            "final_url": final_url,
            "urls": urls,
            "errors": [error for error in history.errors() if error],
            "screenshots": [path for path in history.screenshot_paths() if path],
        }
    finally:
        await browser_session.stop()


def _save_artifact(worker_id: int, job: dict, result: dict) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", job.get("site", "unknown"))[:20].strip("_") or "unknown"
    artifact_path = config.LOG_DIR / f"browser_use_{ts}_w{worker_id}_{slug}.json"
    artifact_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return artifact_path


def _rank_confidence(confidence: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(confidence.lower(), 1)


def _is_permanent_failure(result_status: str) -> bool:
    return result_status in {"manual", "captcha", "expired", "login_issue"}


def _record_confirmation_email(url: str, sent_at: str | None = None, error: str | None = None) -> None:
    conn = get_connection()
    conn.execute(
        """
        UPDATE jobs
        SET confirmation_email_sent_at = ?,
            confirmation_email_error = ?
        WHERE url = ?
        """,
        (sent_at, error, url),
    )
    conn.commit()


def _send_confirmation_email(job: dict, result: dict, artifact_path: Path) -> None:
    profile = config.load_profile()
    automation = profile.get("automation", {})
    if not automation.get("email_confirmation_enabled"):
        return
    if not config.GMAIL_CREDENTIALS_PATH.exists():
        return

    recipient = automation.get("confirmation_email") or profile["personal"].get("email")
    if not recipient:
        return

    email_service = GmailAutomationService()
    body = (
        f"Application submitted.\n\n"
        f"Company: {job.get('site', 'Unknown')}\n"
        f"Role: {job['title']}\n"
        f"Application URL: {job.get('application_url') or job['url']}\n"
        f"Applied at: {datetime.now(timezone.utc).isoformat()}\n"
        f"Confidence: {result.get('confidence', 'low')}\n"
        f"Verification: {result.get('verification', '')}\n"
        f"Artifacts: {artifact_path}\n"
    )
    email_service.send_email(
        to=recipient,
        subject=f"Application submitted: {job['title']} @ {job.get('site', 'Unknown')}",
        body=body,
    )
    _record_confirmation_email(job["url"], sent_at=datetime.now(timezone.utc).isoformat(), error=None)


def run_job(
    job: dict,
    port: int,
    worker_id: int = 0,
    model: str = "gemini-2.5-flash",
    dry_run: bool = False,
    headless: bool = False,
) -> tuple[str, int, str | None, str | None]:
    """Run a single apply worker and return status, duration, artifact path, confidence."""
    worker_dir = reset_worker_dir(worker_id)
    del worker_dir  # Reserved for future per-job file staging.

    update_state(
        worker_id,
        status="applying",
        job_title=job["title"],
        company=job.get("site", ""),
        score=job.get("fit_score", 0),
        start_time=time.time(),
        actions=0,
        last_action="starting browser-use",
    )
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    start = time.time()
    artifact_path: Path | None = None

    try:
        result = asyncio.run(
            _run_browser_agent(
                job,
                port=port,
                model=model,
                dry_run=dry_run,
                worker_id=worker_id,
                headless=headless,
            )
        )
        artifact_path = _save_artifact(worker_id, job, result)
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)

        status = result["status"]
        confidence = result.get("confidence", "low")
        update_state(worker_id, status=status, last_action=f"{status} ({elapsed}s)")
        add_event(f"[W{worker_id}] {status.upper()} ({elapsed}s): {job['title'][:30]}")
        return status, duration_ms, str(artifact_path), confidence
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(exc)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(exc)[:25]}")
        error_artifact = {
            "status": "failed",
            "reason": str(exc),
            "confidence": "low",
            "verification": "exception",
            "final_text": "",
            "final_url": "",
            "urls": [],
            "errors": [str(exc)],
            "screenshots": [],
        }
        artifact_path = _save_artifact(worker_id, job, error_artifact)
        return f"failed:{str(exc)[:100]}", duration_ms, str(artifact_path), "low"


def worker_loop(
    worker_id: int = 0,
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 7,
    headless: bool = False,
    model: str = "gemini-2.5-flash",
    dry_run: bool = False,
    use_main_profile: bool = False,
    chrome_profile: str | None = None,
) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or the queue is empty."""
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="", last_action="waiting for job", actions=0)
        job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)

        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle", last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break
            continue

        empty_polls = 0
        chrome_proc = None
        try:
            apply_url = job.get("application_url") or job["url"]
            add_event(f"[W{worker_id}] Launching Chrome to {apply_url[:40]}...")
            chrome_proc = launch_chrome(
                worker_id,
                port=port,
                headless=headless,
                target_url=apply_url,
                use_main_profile=use_main_profile,
                profile_name_override=chrome_profile,
            )

            result, duration_ms, artifact_path, confidence = run_job(
                job,
                port=port,
                worker_id=worker_id,
                model=model,
                dry_run=dry_run,
                headless=headless,
            )

            if result == "applied":
                mark_result(
                    job["url"],
                    "applied",
                    duration_ms=duration_ms,
                    task_id=artifact_path,
                    verification_confidence=confidence,
                )
                if artifact_path:
                    try:
                        artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
                        _send_confirmation_email(job, artifact, Path(artifact_path))
                    except Exception as exc:
                        _record_confirmation_email(job["url"], error=str(exc))
                applied += 1
                update_state(worker_id, jobs_applied=applied, jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                base_status = result.split(":", 1)[0]
                if base_status == "failed":
                    status = "failed"
                else:
                    status = base_status
                mark_result(
                    job["url"],
                    status,
                    reason,
                    permanent=_is_permanent_failure(status),
                    duration_ms=duration_ms,
                    task_id=artifact_path,
                    verification_confidence=confidence,
                )
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
        except KeyboardInterrupt:
            release_lock(job["url"])
            break
        except Exception as exc:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(exc)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


def main(
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 7,
    headless: bool = False,
    model: str = "gemini-2.5-flash",
    dry_run: bool = False,
    continuous: bool = False,
    poll_interval: int = 60,
    workers: int = 1,
    use_main_profile: bool = False,
    chrome_profile: str | None = None,
) -> None:
    """Launch the browser-use apply pipeline."""
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()
    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    for i in range(workers):
        init_worker(i)

    console.print(f"Launching apply pipeline ({mode_label}, {workers} worker{'s' if workers > 1 else ''}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = stop all workers[/dim]")

    def _sigint_handler(sig, frame):
        del sig, frame
        console.print("\n[red bold]STOPPING[/red bold]")
        _stop_event.set()
        kill_all_chrome()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            dashboard_running = True

            def _refresh():
                while dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    use_main_profile=use_main_profile,
                    chrome_profile=chrome_profile,
                )
            else:
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0) for i in range(workers)]
                else:
                    limits = [0] * workers

                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            use_main_profile=use_main_profile,
                            chrome_profile=chrome_profile,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(item[0] for item in results)
                total_failed = sum(item[1] for item in results)

            dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(f"\n[bold]Done: {total_applied} applied, {total_failed} failed (${totals['cost']:.3f})[/bold]")
        console.print(f"Logs: {config.LOG_DIR}")
    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
        cleanup_on_exit()

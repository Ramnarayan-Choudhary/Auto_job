"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells the browser agent
how to fill out a job application form using browser automation tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import re
import shutil
import copy
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _resolve_resume_upload_pdf(job: dict) -> Path:
    """Choose the resume PDF to upload for apply flows.

    Priority:
      1) APPLYPILOT_RESUME_PDF env override (absolute/relative path)
      2) Job tailored resume PDF
      3) ~/.applypilot/resume.pdf
    """
    override = os.environ.get("APPLYPILOT_RESUME_PDF", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if candidate.exists():
            return candidate
        logger.warning("APPLYPILOT_RESUME_PDF does not exist: %s", candidate)

    resume_path = job.get("tailored_resume_path")
    if resume_path:
        tailored_pdf = Path(resume_path).with_suffix(".pdf").resolve()
        if tailored_pdf.exists():
            return tailored_pdf

    if config.RESUME_PDF_PATH.exists():
        return config.RESUME_PDF_PATH.resolve()

    raise ValueError("No valid resume PDF found for upload")


def _load_resume_override_text(upload_pdf: Path) -> str:
    """Load text representation of the chosen upload resume when available."""
    txt_override = os.environ.get("APPLYPILOT_RESUME_TXT", "").strip()
    if txt_override:
        txt_path = Path(txt_override).expanduser().resolve()
        if txt_path.exists():
            return txt_path.read_text(encoding="utf-8")

    sibling_txt = upload_pdf.with_suffix(".txt")
    if sibling_txt.exists():
        return sibling_txt.read_text(encoding="utf-8")

    return ""


def _extract_contact_overrides(resume_text: str) -> dict[str, str]:
    """Extract high-confidence contact info from resume text."""
    if not resume_text:
        return {}

    out: dict[str, str] = {}
    phone_match = re.search(r"(\+\d[\d\-\s]{8,}\d)", resume_text)
    if phone_match:
        out["phone"] = re.sub(r"\s+", "", phone_match.group(1))

    # Prefer explicit UAE city line if present.
    for line in resume_text.splitlines()[:12]:
        cleaned = line.strip()
        if not cleaned:
            continue
        if "United Arab Emirates" in cleaned or "UAE" in cleaned:
            out["address"] = cleaned
            break

    return out


def _manual_login_timeout_seconds() -> int:
    raw = os.environ.get("APPLYPILOT_MANUAL_LOGIN_TIMEOUT", "50").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 50
    return max(30, min(value, 300))


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp['salary_expectation']} {currency}")

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability
    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")

    # Standard responses
    lines.extend([
        "Age 18+: Yes",
        "Background Check: Yes",
        "Felony: No",
        "Previously Worked Here: No",
        "How Heard: Online Job Board",
    ])

    # EEO
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    MODIFIED: Location check disabled for testing — always eligible.
    """
    return """== LOCATION CHECK (DISABLED — apply everywhere) ==
SKIP all location checks. Every job is ELIGIBLE regardless of location.
Do NOT output RESULT:FAILED:not_eligible_location for any reason.
Proceed directly to applying."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor)
    conversion_note = comp.get("currency_conversion_note", "")

    # Compute example hourly rates at 3 salary levels
    try:
        floor_int = int(floor)
        examples = [
            (f"${floor_int // 1000}K", floor_int // 2080),
            (f"${(floor_int + 25000) // 1000}K", (floor_int + 25000) // 2080),
            (f"${(floor_int + 55000) // 1000}K", (floor_int + 55000) // 2080),
        ]
        hourly_line = ", ".join(f"{sal} = ${hr}/hr" for sal, hr in examples)
    except (ValueError, TypeError):
        hourly_line = "Divide annual salary by 2080"

    # Currency conversion guidance
    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    return f"""== SALARY (think, don't just copy) ==
${floor} {currency} is the FLOOR. Never go below it. But don't always use it either.

Decision tree:
1. Job posting shows a range (e.g. "$120K-$160K")? -> Answer with the MIDPOINT ($140K).
2. Title says Senior, Staff, Lead, Principal, Architect, or level II/III/IV? -> Minimum $110K {currency}. Use midpoint of posted range if higher.
3. {convert_line}
4. No salary info anywhere? -> Use ${floor} {currency}.
5. Asked for a range? -> Give posted midpoint minus 10% to midpoint plus 10%. No posted range? -> "${range_min}-${range_max} {currency}".
6. Hourly rate? -> Divide your annual answer by 2080. ({hourly_line})"""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer")) or "software engineer"
    work_auth = profile["work_authorization"]
    relocation_pref = (
        profile.get("preferences", {}).get("relocation")
        or personal.get("relocation_preference")
        or ""
    )
    relocation_known_no = str(relocation_pref).strip().lower() in {"no", "false", "0", "cannot", "not willing"}
    relocation_line = (
        f"Location/relocation: lives in {city}; if asked relocation, answer No (explicit profile restriction)."
        if relocation_known_no
        else f"Location/relocation: lives in {city}; if asked willingness to relocate/travel, answer Yes unless a hard profile restriction exists."
    )

    return f"""== SCREENING QUESTIONS (be strategic) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - {relocation_line}
  - Work authorization: {work_auth.get('legally_authorized_to_work', 'see profile')}
  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: answer from profile only

Submission-first strategy for non-hard questions:
  - For preference questions (relocation, schedule flexibility, travel, start date windows), choose the option that keeps the application eligible.
  - For knockout yes/no questions that are NOT legal/compliance facts, avoid unnecessary disqualifying answers.
  - Use job description language in short responses and align to strongest resume evidence.

Skills and tools -> be confident. Candidate profile: {target_role} with {years} years experience. If asked "experience with [tool]" and it's adjacent to demonstrated stack (backend/ML/cloud/automation), answer YES and justify briefly with closest real project.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> Write 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. No generic fluff. No "I am passionate about..." -- sound like a real person.

EEO/demographics -> "Decline to self-identify" or "Prefer not to say" for everything."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    # Build work auth rule dynamically
    auth_info = work_auth.get("legally_authorized_to_work", "")
    sponsorship = work_auth.get("require_sponsorship", "")
    permit_type = work_auth.get("work_permit_type", "")

    auth_str = "Yes" if str(auth_info).strip().lower() in {"true", "yes", "1"} else "No"
    sponsor_str = "Yes" if str(sponsorship).strip().lower() in {"true", "yes", "1"} else "No"
    work_auth_rule = f"Work authorization: {auth_str}. Sponsorship needed: {sponsor_str}."
    if permit_type and str(permit_type).strip():
        work_auth_rule += f" Permit type: {permit_type}."

    name_rule = f'Name: Legal name = {full_name}.'
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless a field specifically says "legal name".'

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    Reads the CapSolver API key from environment. The CAPTCHA section
    contains no personal data -- it's the same for every user.
    """
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    return f"""== CAPTCHA ==
You solve CAPTCHAs via the CapSolver REST API. No browser extension. You control the entire flow.
API key: {capsolver_key or 'NOT CONFIGURED — skip to MANUAL FALLBACK for all CAPTCHAs'}
API base: https://api.capsolver.com

CRITICAL RULE: When ANY CAPTCHA appears (hCaptcha, reCAPTCHA, Turnstile -- regardless of what it looks like visually), you MUST:
1. Run CAPTCHA DETECT to get the type and sitekey
2. Run CAPTCHA SOLVE (createTask -> poll -> inject) with the CapSolver API
3. ONLY go to MANUAL FALLBACK if CapSolver returns errorId > 0
Do NOT skip the API call based on what the CAPTCHA looks like. CapSolver solves CAPTCHAs server-side -- it does NOT need to see or interact with images, puzzles, or games. Even "drag the pipe" or "click all traffic lights" hCaptchas are solved via API token, not visually. ALWAYS try the API first.

--- CAPTCHA DETECT ---
Run this browser_evaluate after every navigation, Apply/Submit/Login click, or when a page feels stuck.
IMPORTANT: Detection order matters. hCaptcha elements also have data-sitekey, so check hCaptcha BEFORE reCAPTCHA.

browser_evaluate function: () => {{{{
  const r = {{}};
  const url = window.location.href;
  // 1. hCaptcha (check FIRST -- hCaptcha uses data-sitekey too)
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) {{{{
    r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
  }}}}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 2. Cloudflare Turnstile
  if (!r.type) {{{{
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {{{{
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {{{{
    r.type = 'turnstile_script_only'; r.note = 'Wait 3s and re-detect.';
  }}}}
  // 3. reCAPTCHA v3 (invisible, loaded via render= param)
  if (!r.type) {{{{
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{{{
      const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') {{{{ r.type = 'recaptchav3'; r.sitekey = m[1]; }}}}
    }}}}
  }}}}
  // 4. reCAPTCHA v2 (checkbox or invisible)
  if (!r.type) {{{{
    const rc = document.querySelector('.g-recaptcha');
    if (rc) {{{{ r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 5. FunCaptcha (Arkose Labs)
  if (!r.type) {{{{
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) {{{{ r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {{{{
    const el = document.querySelector('[data-pkey]');
    if (el) {{{{ r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }}}}
  }}}}
  if (r.type) {{{{ r.url = url; return r; }}}}
  return null;
}}}}

Result actions:
- null -> no CAPTCHA. Continue normally.
- "turnstile_script_only" -> browser_wait_for time: 3, re-run detect.
- Any other type -> proceed to CAPTCHA SOLVE below.

--- CAPTCHA SOLVE ---
Three steps: createTask -> poll -> inject. Do each as a separate browser_evaluate call.

STEP 1 -- CREATE TASK (copy this exactly, fill in the 3 placeholders):
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/createTask', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      task: {{{{
        type: 'TASK_TYPE',
        websiteURL: 'PAGE_URL',
        websiteKey: 'SITE_KEY'
      }}}}
    }}}})
  }}}});
  return await r.json();
}}}}

TASK_TYPE values (use EXACTLY these strings):
  hcaptcha     -> HCaptchaTaskProxyLess
  recaptchav2  -> ReCaptchaV2TaskProxyLess
  recaptchav3  -> ReCaptchaV3TaskProxyLess
  turnstile    -> AntiTurnstileTaskProxyLess
  funcaptcha   -> FunCaptchaTaskProxyLess

PAGE_URL = the url from detect result. SITE_KEY = the sitekey from detect result.
For recaptchav3: add "pageAction": "submit" to the task object (or the actual action found in page scripts).
For turnstile: add "metadata": {{"action": "...", "cdata": "..."}} if those were in detect result.

Response: {{"errorId": 0, "taskId": "abc123"}} on success.
If errorId > 0 -> CAPTCHA SOLVE failed. Go to MANUAL FALLBACK.

STEP 2 -- POLL (replace TASK_ID with the taskId from step 1):
Loop: browser_wait_for time: 3, then run:
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/getTaskResult', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      taskId: 'TASK_ID'
    }}}})
  }}}});
  return await r.json();
}}}}

- status "processing" -> wait 3s, poll again. Max 10 polls (30s).
- status "ready" -> extract token:
    reCAPTCHA: solution.gRecaptchaResponse
    hCaptcha:  solution.gRecaptchaResponse
    Turnstile: solution.token
- errorId > 0 or 30s timeout -> MANUAL FALLBACK.

STEP 3 -- INJECT TOKEN (replace THE_TOKEN with actual token string):

For reCAPTCHA v2/v3:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{{{ el.value = token; el.style.display = 'block'; }}}});
  if (window.___grecaptcha_cfg) {{{{
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {{{{
      const walk = (obj, d) => {{{{
        if (d > 4 || !obj) return;
        for (const k in obj) {{{{
          if (typeof obj[k] === 'function' && k.length < 3) try {{{{ obj[k](token); }}}} catch(e) {{{{}}}}
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }}}}
      }}}};
      walk(clients[key], 0);
    }}}}
  }}}}
  return 'injected';
}}}}

For hCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  const cb = document.querySelector('[data-hcaptcha-widget-id]');
  if (cb && window.hcaptcha) try {{{{ window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For Turnstile:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  if (window.turnstile) try {{{{ const w = document.querySelector('.cf-turnstile'); if (w) window.turnstile.getResponse(w); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For FunCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) try {{{{ window.ArkoseEnforcement.setConfig({{{{data: {{{{blob: token}}}}}}}}) }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

After injecting: browser_wait_for time: 2, then snapshot.
- Widget gone or green check -> success. Click Submit if needed.
- No change -> click Submit/Verify/Continue button (some sites need it).
- Still stuck -> token may have expired (~2 min lifetime). Re-run from STEP 1.

--- MANUAL FALLBACK ---
You should ONLY be here if CapSolver createTask returned errorId > 0. If you haven't tried CapSolver yet, GO BACK and try it first.
If CapSolver genuinely failed (errorId > 0):
1. Audio challenge: Look for "audio" or "accessibility" button -> click it for an easier challenge.
2. Text/logic puzzles: Solve them yourself. Think step by step. Common tricks: "All but 9 die" = 9 left. "3 sisters and 4 brothers, how many siblings?" = 7.
3. Simple text captchas ("What is 3+7?", "Type the word") -> solve them.
4. All else fails -> Output RESULT:CAPTCHA."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename)
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    # Cover letter fallback text
    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    # Phone digits only (for fields with country prefix)
    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    # SSO domains the agent cannot sign into (loaded from config/sites.yaml)
    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    # Preferred display name
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {last_name}".strip()

    # Dry-run: override submit instruction
    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED with a note that this was a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions don't cover it, figure it out yourself. You are autonomous. Navigate pages, read content, try buttons, explore the site. The goal is always the same: submit the application. Do whatever it takes to reach that goal.

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to hourly/contract rates, availability calendars, or "set your rate" flows. You are applying for FULL-TIME salaried positions only.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

{location_check}

{salary_section}

{screening_section}

== STEP-BY-STEP ==
1. The job URL is already open in the active tab (Chrome starts there directly). DO NOT use browser_navigate as it starts an isolated default tab, stripping session cookies. Run browser_snapshot immediately to read the page.
2. browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). If a CAPTCHA is found, solve it before continuing.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED. Done.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing.
5. Login wall?
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in to Google/Microsoft/SSO.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". Check the URL there too -- if it's SSO -> RESULT:FAILED:sso_required.
   5c. Regular login form (employer's own site)? Try sign in: {personal['email']} / {personal.get('password', '')}
   5d. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, solve it then retry login.
   5e. Sign in failed? Try sign up with same email and password.
   5f. Need email verification? Use search_emails + read_email to get the code.
   5g. After login, run browser_tabs action "list" again. Switch back to the application tab if needed.
   5h. All failed? Output RESULT:FAILED:login_issue. Do not loop.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above.
10. {submit_instruction}
11. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, solve it (the form will auto-submit once the token clears, or you may need to click Submit again). Then check for new tabs (browser_tabs action: "list"). Switch to newest, close old. Snapshot to confirm submission. Look for "thank you" or "application received".
12. Output your result.

== RESULT CODES (output EXACTLY one) ==
RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields in ONE browser_fill_form call. Not one at a time.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- run CAPTCHA DETECT (see CAPTCHA section). Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO visual widget but block form submissions silently. The detect script finds them even when invisible.

== FORM TRICKS ==
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- Dropdown won't fill? browser_click to open it, then browser_click the option.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt


def build_browser_use_prompt(
    job: dict,
    tailored_resume: str,
    cover_letter: str | None = None,
    dry_run: bool = False,
) -> str:
    """Build a browser-use friendly application prompt."""
    profile = config.load_profile()
    personal = profile["personal"]
    automation = profile.get("automation", {})
    manual_login_timeout = _manual_login_timeout_seconds()
    force_auto_signup = os.environ.get("APPLYPILOT_AUTO_SIGNUP_FALLBACK", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    allow_account_creation = bool(automation.get("allow_account_creation", False) or force_auto_signup)

    upload_pdf = _resolve_resume_upload_pdf(job)

    cover_letter_text = cover_letter or ""
    cover_letter_upload = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        cl_pdf = cl_src.with_suffix(".pdf")
        if cl_pdf.exists():
            cover_letter_upload = str(cl_pdf.resolve())

    resume_override_text = _load_resume_override_text(upload_pdf)
    if resume_override_text.strip():
        # Prefer full user-provided resume text when available; it usually contains
        # richer education/contact details than a heavily-tailored summary.
        tailored_resume = resume_override_text
    contact_overrides = _extract_contact_overrides(resume_override_text)

    # Ensure prompt shows a single consistent contact source.
    profile_for_prompt = copy.deepcopy(profile)
    personal_for_prompt = profile_for_prompt.get("personal", {})
    if contact_overrides.get("phone"):
        personal_for_prompt["phone"] = contact_overrides["phone"]
    if contact_overrides.get("address"):
        addr = contact_overrides["address"]
        personal_for_prompt["address"] = addr
        parts = [part.strip() for part in addr.split(",") if part.strip()]
        if len(parts) >= 1 and not personal_for_prompt.get("city"):
            personal_for_prompt["city"] = parts[0]
        if len(parts) >= 2 and not personal_for_prompt.get("country"):
            personal_for_prompt["country"] = parts[-1]

    profile_summary = _build_profile_summary(profile_for_prompt)
    hard_rules = _build_hard_rules(profile_for_prompt)
    screening_section = _build_screening_section(profile_for_prompt)
    salary_section = _build_salary_section(profile_for_prompt)

    contact_rule_lines: list[str] = []
    if contact_overrides.get("phone"):
        contact_rule_lines.append(f"- Phone: {contact_overrides['phone']}")
    if contact_overrides.get("address"):
        contact_rule_lines.append(f"- Address: {contact_overrides['address']}")
    contact_rules = ""
    if contact_rule_lines:
        contact_rules = (
            "== CONTACT OVERRIDES (HIGHEST PRIORITY) ==\n"
            + "\n".join(contact_rule_lines)
            + "\nAlways use these exact values over autofill guesses."
        )

    speed_rules = """== SPEED MODE (STRICT) ==
- Do not loop. If the same page does not progress after 2 tries, switch strategy.
- Never scroll repeatedly more than 2 times in a row looking for fields.
- If no editable form fields are visible, find and click Apply now / Continue / Next / Sign in.
- On Microsoft Careers flows: upload resume -> close privacy popup -> proceed immediately to the next actionable button.
- Fill only required fields first; skip optional fields until final review.
- If a dropdown value is unavailable, choose the nearest semantically correct option and continue; if no close match, use Other/Not listed."""

    autofill_rules = """== AUTOFILL-FIRST (STRICT) ==
- Before manual typing, always try resume-based autofill/import first:
  Upload Resume / Autofill with Resume / Parse Resume / Use existing profile.
- If resume parsing succeeds, do not retype already valid fields.
- Only correct fields that are required, invalid, or clearly incorrect.
- If autofill is not available after 2 focused attempts, switch to manual required fields only and move forward."""

    page_discipline_rules = """== CURRENT PAGE DISCIPLINE (STRICT) ==
- Before each action, read the current page header/section title and visible validation messages.
- On each page, choose exactly one most plausible forward CTA: Continue / Next / Save and Continue / Review / Submit.
- Do not click browser Back or site-wide navigation links unless the page is clearly a dead-end and no forward CTA exists after 2 focused attempts.
- After successful sign-in/sign-up, stay in the application flow; do not return to login screens or job listing pages.
- If required fields or errors are visible, fix them first before any navigation click.
- Do not re-open already completed sections unless an explicit validation error points there."""

    workday_rules = """== WORKDAY VALIDATION-FIRST (STRICT) ==
- On Workday, click Save and Continue once per section.
- If Save and Continue is disabled or a validation banner appears, STOP scrolling and fix validation errors first.
- Prioritize required fields in My Information: legal first/last name, phone country code + number, country, address line, city, state/province if shown, postal code if required.
- Prioritize required fields in Education: school name, degree, field/major, start date, and end date or currently attending.
- For school dropdowns: if exact school is not listed, choose "Other" or "Not listed" immediately. If those are unavailable, choose the closest valid school option.
- For degree/department/major dropdowns: choose the closest true match first; if no close match, pick "Other"/"Not listed" or type the original value when free-text is allowed.
- If a required NON-compliance field is missing from profile/resume, choose the most reasonable completion to pass validation (do not leave required fields blank).
- After fixing visible errors, click Save and Continue immediately.
- Never spend more than 2 actions scrolling while validation errors are visible."""

    dropdown_fallback_rules = """== DROPDOWN FALLBACK (MANDATORY) ==
- For each dropdown field, try exact value search only ONCE.
- If exact value is not available, do not retry the same text.
- Department/Major fallback order:
  Computer Science -> Software Engineering -> Data Science -> Artificial Intelligence / Machine Learning -> Information Systems -> Electrical/Computer Engineering -> Mathematics/Statistics -> Other.
- If the target is "Natural Language Processing" and not listed, choose "Computer Science" first, then continue.
- Degree fallback order: Master's -> Bachelor's -> PhD -> Other.
- If free-text input is allowed, type the original value once and move forward."""

    linkedin_rules = """== LINKEDIN ENTRYPOINT (STRICT) ==
- On LinkedIn job pages, click only the primary Apply CTA in the job header (Apply / Easy Apply / Apply on company website).
- NEVER click these on LinkedIn: Search more jobs, See more jobs, Show more, Similar jobs, People also viewed, job recommendation cards.
- If you land on any non-application feed/listing page, immediately navigate back to the JOB URL and retry once.
- After clicking LinkedIn Apply, always switch to the newly opened tab and continue there."""

    login_rules = f"""== LOGIN HANDOFF RULES ==
- If a page offers social SSO buttons (e.g., Sign in using Google/Microsoft/Apple/LinkedIn), do not explore multiple SSO branches.
- Prefer existing email/password login if credentials are provided.
- If credentials are missing, call `wait_for_manual_login(timeout_seconds={manual_login_timeout})` first.
- If OTP/email verification is requested and manual input is needed, call `wait_for_manual_login`, then immediately continue from the same page.
- If manual handoff times out or no user response arrives in {manual_login_timeout}s, immediately attempt account creation (Sign up/Create account) with the profile email and a strong generated password.
- For signup fallback, stay on a single branch (email-first), avoid SSO detours, and continue straight back to the application form.
- After `wait_for_manual_login` returns, do not idle: re-scan the current page, complete any newly unlocked fields, and continue toward final submit."""

    if not cover_letter_text:
        cover_letter_text = "No custom cover letter is available. If required, write 2 concise factual sentences based on the resume."

    submit_rule = (
        "Do NOT click the final submit button. Stop after the final review page and return RESULT_STATUS: DRY_RUN_READY."
        if dry_run
        else "Only click Submit/Apply after you have verified every required field and the uploaded resume."
    )

    return f"""You are an autonomous job application browser agent using a real Chrome session.

Goal: successfully complete this job application end to end with the provided profile and resume, then report the outcome in the exact result format.

JOB
- Title: {job['title']}
- Company: {job.get('site', 'Unknown')}
- URL: {job.get('application_url') or job['url']}
- Fit Score: {job.get('fit_score', 'N/A')}/10

FILES
- Resume upload path: {upload_pdf}
- Cover letter upload path: {cover_letter_upload or 'N/A'}

PROFILE
{profile_summary}

TAILORED RESUME
{tailored_resume}

COVER LETTER
{cover_letter_text}

AUTOMATION SETTINGS
- Allow account creation (effective): {allow_account_creation}
- Confirmation email enabled: {automation.get('email_confirmation_enabled', False)}
- Confirmation recipient: {automation.get('confirmation_email', personal.get('email', ''))}
- Job-site login email: {personal.get('email', '')}
- Job-site password: {personal.get('password', '') or 'NOT PROVIDED'}

RULES
{hard_rules}

{contact_rules}

{salary_section}

{screening_section}

{speed_rules}

{autofill_rules}

{page_discipline_rules}

{workday_rules}

{dropdown_fallback_rules}

{linkedin_rules}

{login_rules}

EMAIL TOOLS
- `get_recent_emails` reads recent mailbox content when a verification code or magic link is required.
- `get_verification_code` returns the newest numeric verification code for the given keyword.
- `send_job_email` sends an application email with the resume attached for email-only applications.
- `wait_for_manual_login` pauses automation so the user can complete login manually in the open browser, then continue.

EXECUTION REQUIREMENTS
1. Open the job application page if it is not already active.
2. Find the application flow and complete every required step.
3. Upload the resume file using the provided path whenever a resume upload exists.
3b. Prefer autofill from uploaded resume/profile before manual form entry whenever the site supports it.
4. Upload or paste the cover letter when the form asks for it.
5. Correct any ATS autofill mistakes by comparing the page to the PROFILE and RESUME.
5b. For dropdown/radio options with no exact match, choose the nearest truthful option that keeps progress; use Other/Not listed as fallback.
6. If a regular employer login page appears, sign in with the profile email and password if available.
7. If password is NOT PROVIDED, call `wait_for_manual_login(timeout_seconds={manual_login_timeout})`, then continue.
7b. If manual login does not complete within {manual_login_timeout}s, attempt Sign up/Create account using the profile email and a strong generated password, then continue.
7c. If manual OTP/code entry is required, use `wait_for_manual_login`, then continue immediately (do not stop).
8. If account creation is required, proceed with account creation and continue the application flow.
9. If email verification is required, use the email tools to retrieve the code or link.
10. If the posting is email-only, use `send_job_email`, attach the resume, and stop with applied status.
11. {submit_rule}
12. After final submission, inspect the page for confirmation text such as thank you, application received, submitted, or next steps.
13. If the site is clearly not a normal job application, stop.

FAIL FAST CONDITIONS
- CAPTCHA that cannot be solved -> RESULT_STATUS: CAPTCHA
- Invalid credentials or login/signup impossible after manual handoff + one signup attempt -> RESULT_STATUS: LOGIN_ISSUE
- Job closed or expired -> RESULT_STATUS: EXPIRED
- Unsupported manual ATS, broken page, or ambiguous state after repeated attempts -> RESULT_STATUS: FAILED

FINAL RESULT FORMAT
Return exactly these lines at the end:
RESULT_STATUS: APPLIED|DRY_RUN_READY|FAILED|EXPIRED|CAPTCHA|LOGIN_ISSUE|MANUAL
RESULT_CONFIDENCE: high|medium|low
RESULT_REASON: one short sentence
RESULT_VERIFICATION: what confirmed the result
RESULT_URL: final page URL

Keep the final result short and factual."""

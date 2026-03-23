"""AutoJob HTML Dashboard Generator.

Generates a self-contained HTML dashboard with:
  - Summary stats (total, enriched, scored, high-fit)
  - Score distribution bar chart
  - Jobs-by-source breakdown
  - Filterable job cards grouped by score
  - Client-side search and score filtering
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rich.console import Console

from autojob.config import APP_DIR, LOG_DIR
from autojob.database import get_connection

console = Console()


def generate_dashboard(output_path: str | None = None, interactive: bool = False) -> str:
    """Generate an HTML dashboard of all jobs with fit scores.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.autojob/dashboard.html.
        interactive: If True, render buttons that call localhost API endpoints
            to trigger internal apply runs.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"

    conn = get_connection()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]
    high_fit = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7"
    ).fetchone()[0]

    # Score distribution
    score_dist: dict[int, int] = {}
    if scored:
        rows = conn.execute(
            "SELECT fit_score, COUNT(*) FROM jobs "
            "WHERE fit_score IS NOT NULL "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        for r in rows:
            score_dist[r[0]] = r[1]

    # Site stats
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # All scored jobs (5+), ordered by score desc
    jobs = conn.execute("""
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning
        FROM jobs
        WHERE fit_score >= 5
        ORDER BY fit_score DESC, site, title
    """).fetchall()

    # Color map per site
    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "Job Bank Canada": "#3b82f6", "CareerJet Canada": "#8b5cf6",
        "Hacker News Jobs": "#ff6600", "BuiltIn Remote": "#ec4899",
        "TD Bank": "#00a651", "CIBC": "#c41f3e", "RBC": "#003168",
        "indeed": "#2164f3", "linkedin": "#0a66c2",
        "Dice": "#eb1c26", "Glassdoor": "#0caa41",
    }

    # Score distribution bar chart
    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, 0, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
        score_bars += f"""
        <div class="score-row">
          <span class="score-label">{s}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""

    # Site stats rows
    site_rows = ""
    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        avg = s["avg_score"] or 0
        site_rows += f"""
        <div class="site-row">
          <div class="site-name" style="color:{color}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; avg score {avg}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </div>"""

    # Job cards grouped by score
    job_sections = ""
    current_score = None
    for j in jobs:
        score = j["fit_score"] or 0
        if score != current_score:
            if current_score is not None:
                job_sections += "</div>"
            score_color = "#10b981" if score >= 7 else "#f59e0b"
            score_label = {
                10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                7: "Good Fit", 6: "Moderate+", 5: "Moderate",
            }.get(score, f"Score {score}")
            count_at_score = score_dist.get(score, 0)
            job_sections += f"""
            <h2 class="score-header" style="border-color:{score_color}">
              <span class="score-badge" style="background:{score_color}">{score}</span>
              {score_label} ({count_at_score} jobs)
            </h2>
            <div class="job-grid">"""
            current_score = score

        title = escape(j["title"] or "Untitled")
        url = escape(j["url"] or "")
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "")
        site_color = colors.get(j["site"] or "", "#6b7280")
        apply_url = escape(j["application_url"] or "")

        # Parse keywords and reasoning from score_reasoning
        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        desc_preview = escape(j["full_description"] or "")[:300]
        full_desc_html = escape(j["full_description"] or "").replace("\n", "<br>")
        desc_len = len(j["full_description"] or "")

        meta_parts = []
        meta_parts.append(
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>'
        )
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:40]}</span>')
        meta_html = " ".join(meta_parts)

        apply_html = ""
        if apply_url:
            if interactive:
                apply_html = (
                    f'<a href="{apply_url}" class="apply-link" target="_blank">Open</a>'
                    f'<button type="button" class="apply-agent-btn" data-url="{apply_url}">Apply via Agent</button>'
                    f'<span class="apply-run-status" data-url="{apply_url}"></span>'
                )
            else:
                apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank">Apply</a>'

        job_sections += f"""
        <div class="job-card" data-score="{score}" data-site="{escape(j['site'] or '')}" data-location="{location.lower()}" data-apply-url="{apply_url}">
          <div class="card-header">
            <span class="score-pill" style="background:{'#10b981' if score >= 7 else '#f59e0b'}">{score}</span>
            <a href="{url}" class="job-title" target="_blank">{title}</a>
          </div>
          <div class="meta-row">{meta_html}</div>
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          <p class="desc-preview">{desc_preview}...</p>
          {"<details class='full-desc-details'><summary class='expand-btn'>Full Description (" + f'{desc_len:,}' + " chars)</summary><div class='full-desc'>" + full_desc_html + "</div></details>" if j["full_description"] else ""}
          <div class="card-footer">{apply_html}</div>
        </div>"""

    if current_score is not None:
        job_sections += "</div>"

    interactive_js = ""
    if interactive:
        interactive_js = """
const _runState = {};

async function startApplyForButton(btn) {
  const url = btn.dataset.url;
  if (!url) return;
  btn.disabled = true;
  btn.textContent = 'Starting...';

  try {
    const res = await fetch('/api/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const payload = await res.json();
    if (!res.ok || !payload.ok) {
      const msg = payload.error || `HTTP ${res.status}`;
      btn.disabled = false;
      btn.textContent = 'Apply via Agent';
      const statusEl = document.querySelector(`.apply-run-status[data-url="${CSS.escape(url)}"]`);
      if (statusEl) statusEl.textContent = `Error: ${msg}`;
      return;
    }
    await refreshRunStates();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Apply via Agent';
    const statusEl = document.querySelector(`.apply-run-status[data-url="${CSS.escape(url)}"]`);
    if (statusEl) statusEl.textContent = 'Network error';
  }
}

function applyRunUi(url, run) {
  const btn = document.querySelector(`.apply-agent-btn[data-url="${CSS.escape(url)}"]`);
  const statusEl = document.querySelector(`.apply-run-status[data-url="${CSS.escape(url)}"]`);
  if (!btn || !statusEl) return;

  const dbStatus = (run.db_apply_status || '').toLowerCase();
  const isRunning = run.state === 'running';
  if (isRunning || dbStatus === 'in_progress') {
    btn.disabled = true;
    btn.textContent = 'Applying...';
    statusEl.textContent = 'In progress';
    return;
  }

  btn.disabled = false;
  btn.textContent = 'Apply via Agent';

  if (dbStatus === 'applied') {
    statusEl.textContent = 'Applied';
  } else if (dbStatus === 'failed') {
    statusEl.textContent = 'Failed';
  } else if (dbStatus === 'manual') {
    statusEl.textContent = 'Manual';
  } else if (run.state === 'finished') {
    statusEl.textContent = 'Run finished';
  } else {
    statusEl.textContent = '';
  }
}

async function refreshRunStates() {
  try {
    const res = await fetch('/api/runs');
    if (!res.ok) return;
    const payload = await res.json();
    const runs = payload.runs || [];
    for (const run of runs) {
      _runState[run.url] = run;
      applyRunUi(run.url, run);
    }
  } catch (_) {}
}

document.addEventListener('click', (e) => {
  if (!e.target.classList.contains('apply-agent-btn')) return;
  startApplyForButton(e.target);
});

setInterval(refreshRunStates, 3000);
refreshRunStates();
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoJob Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }}

  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 2rem; }}

  /* Summary cards */
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 2.5rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}

  /* Filters */
  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 200px; }}
  .search-input::placeholder {{ color: #64748b; }}

  /* Score distribution */
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }}
  .score-dist {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .score-dist h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}

  /* Site bars */
  .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .site-row {{ margin-bottom: 0.8rem; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}

  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: #0f172a; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}

  .job-title {{ color: #e2e8f0; text-decoration: none; font-weight: 600; font-size: 0.95rem; }}
  .job-title:hover {{ color: #60a5fa; }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: #60a5fa22; }}
  .apply-agent-btn {{ font-size: 0.78rem; color: #0f172a; background: #22c55e; border: none; padding: 0.34rem 0.72rem; border-radius: 6px; font-weight: 600; cursor: pointer; }}
  .apply-agent-btn:hover {{ background: #16a34a; }}
  .apply-agent-btn[disabled] {{ cursor: not-allowed; opacity: 0.7; }}
  .apply-run-status {{ font-size: 0.72rem; color: #94a3b8; min-width: 6rem; text-align: right; }}

  /* Expandable full description */
  .full-desc-details {{ margin-bottom: 0.75rem; }}
  .expand-btn {{ font-size: 0.8rem; color: #60a5fa; cursor: pointer; list-style: none; padding: 0.3rem 0; }}
  .expand-btn::-webkit-details-marker {{ display: none; }}
  .expand-btn:hover {{ color: #93c5fd; }}
  .full-desc {{ font-size: 0.8rem; color: #cbd5e1; line-height: 1.6; margin-top: 0.5rem; padding: 0.75rem; background: #0f172a; border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}

  @media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .job-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 1rem; }}
  }}
</style>
</head>
<body>

<h1>AutoJob Dashboard</h1>
<p class="subtitle">{total} jobs &middot; {scored} scored &middot; {high_fit} strong matches (7+)</p>

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{ready}</div><div class="stat-label">Ready (desc + URL)</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{scored}</div><div class="stat-label">Scored by LLM</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
</div>

<div class="filters">
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0)">All 5+</button>
  <button class="filter-btn" onclick="filterScore(7)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9)">9+ Perfect</button>
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" class="search-input" placeholder="Filter by title, site..." oninput="filterText(this.value)">
</div>

<div class="score-section">
  <div class="score-dist">
    <h3>Score Distribution</h3>
    {score_bars}
  </div>
  <div class="sites-section">
    <h3>By Source</h3>
    {site_rows}
  </div>
</div>

<div id="job-count" class="job-count"></div>

{job_sections}

<script>
let minScore = 0;
let searchText = '';

function filterScore(min) {{
  minScore = min;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  applyFilters();
}}

function applyFilters() {{
  let shown = 0;
  let total = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    total++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    const scoreMatch = score >= (minScore || 5);
    const textMatch = !searchText || text.includes(searchText);
    if (scoreMatch && textMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  document.getElementById('job-count').textContent = `Showing ${{shown}} of ${{total}} jobs`;

  // Hide empty score groups
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    }}
  }});
}}

applyFilters();
{interactive_js}
</script>

</body>
</html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.autojob/dashboard.html.
    """
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")


def _slugify_for_log(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")
    return cleaned[:80] or "job"


def _job_status_snapshot(url: str) -> dict[str, str]:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT apply_status, apply_error, applied_at
        FROM jobs
        WHERE url = ? OR application_url = ?
        LIMIT 1
        """,
        (url, url),
    ).fetchone()
    if not row:
        return {"apply_status": "", "apply_error": "", "applied_at": ""}
    return {
        "apply_status": row["apply_status"] or "",
        "apply_error": row["apply_error"] or "",
        "applied_at": row["applied_at"] or "",
    }


def serve_dashboard(
    host: str = "127.0.0.1",
    port: int = 8765,
    headless: bool = False,
    model: str = "gemini-2.5-flash",
    min_score: int = 7,
    use_main_profile: bool = False,
    chrome_profile: str | None = None,
) -> None:
    """Serve an interactive dashboard with per-job Apply buttons.

    Clicking `Apply via Agent` starts an internal `autojob apply --url <job>`
    process in the background and the dashboard polls run/status updates.
    """

    runs: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def _refresh_runs_locked() -> None:
        for entry in runs.values():
            proc: subprocess.Popen[str] = entry["process"]
            if entry.get("state") == "running":
                rc = proc.poll()
                if rc is not None:
                    entry["state"] = "finished"
                    entry["returncode"] = rc
                    entry["finished_at"] = int(time.time())
                    handle = entry.get("stdout_handle")
                    if handle:
                        try:
                            handle.flush()
                            handle.close()
                        except Exception:
                            pass
                        entry["stdout_handle"] = None

    def _build_apply_cmd(url: str) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "autojob.cli",
            "apply",
            "--limit",
            "1",
            "--url",
            url,
            "--min-score",
            str(min_score),
            "--model",
            model,
        ]
        if headless:
            cmd.append("--headless")
        if use_main_profile:
            cmd.append("--use-main-profile")
        if chrome_profile:
            cmd.extend(["--chrome-profile", chrome_profile])
        return cmd

    def _start_apply(url: str) -> tuple[bool, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False, "Invalid URL"

        with lock:
            _refresh_runs_locked()
            existing = runs.get(url)
            if existing and existing.get("state") == "running":
                return False, "Apply already running for this job"

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            log_file = LOG_DIR / f"dashboard_apply_{ts}_{_slugify_for_log(url)}.log"
            out_handle = log_file.open("w", encoding="utf-8")
            cmd = _build_apply_cmd(url)
            proc = subprocess.Popen(
                cmd,
                stdout=out_handle,
                stderr=subprocess.STDOUT,
                cwd=str(Path.cwd()),
                env=os.environ.copy(),
                text=True,
            )
            runs[url] = {
                "url": url,
                "state": "running",
                "pid": proc.pid,
                "started_at": int(time.time()),
                "finished_at": None,
                "returncode": None,
                "log_path": str(log_file),
                "command": cmd,
                "process": proc,
                "stdout_handle": out_handle,
            }
            return True, "started"

    def _runs_payload() -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        with lock:
            _refresh_runs_locked()
            for url, entry in runs.items():
                snap = _job_status_snapshot(url)
                payload.append(
                    {
                        "url": url,
                        "state": entry.get("state", "unknown"),
                        "pid": entry.get("pid"),
                        "started_at": entry.get("started_at"),
                        "finished_at": entry.get("finished_at"),
                        "returncode": entry.get("returncode"),
                        "log_path": entry.get("log_path", ""),
                        "db_apply_status": snap["apply_status"],
                        "db_apply_error": snap["apply_error"],
                        "db_applied_at": snap["applied_at"],
                    }
                )
        payload.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
        return payload

    class _Handler(BaseHTTPRequestHandler):
        def _write_json(self, status_code: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _write_html(self, html: str) -> None:
            encoded = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            # Keep terminal output clean; apply subprocess logs already go to files.
            del format, args

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                dashboard_path = generate_dashboard(APP_DIR / "dashboard_live.html", interactive=True)
                html = Path(dashboard_path).read_text(encoding="utf-8")
                self._write_html(html)
                return
            if path == "/api/runs":
                self._write_json(200, {"ok": True, "runs": _runs_payload()})
                return
            if path == "/api/health":
                self._write_json(200, {"ok": True})
                return
            self._write_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/apply":
                self._write_json(404, {"ok": False, "error": "Not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._write_json(400, {"ok": False, "error": "Invalid JSON payload"})
                return

            url = str(payload.get("url") or "").strip()
            if not url:
                self._write_json(400, {"ok": False, "error": "Missing url"})
                return

            started, message = _start_apply(url)
            status_code = 200 if started else 409
            self._write_json(
                status_code,
                {
                    "ok": started,
                    "message": message,
                    "runs": _runs_payload(),
                },
            )

    server = ThreadingHTTPServer((host, port), _Handler)
    bind_url = f"http://{host}:{port}"
    open_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    open_url = f"http://{open_host}:{port}"

    console.print(f"[green]Dashboard server running:[/green] {bind_url}")
    console.print("[dim]Press Ctrl+C to stop[/dim]")
    webbrowser.open(open_url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with lock:
            _refresh_runs_locked()
            for entry in runs.values():
                proc: subprocess.Popen[str] = entry["process"]
                if proc.poll() is None:
                    proc.terminate()

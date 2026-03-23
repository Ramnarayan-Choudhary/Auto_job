#!/usr/bin/env python3
"""Local dashboard server with one-click Apply API.

Serves the existing dashboard UI and adds:
  - POST /api/apply     -> launch one AutoJob run for a specific URL
  - POST /api/stop      -> stop one running AutoJob run (by url or pid)
  - POST /api/stop-all  -> stop all running runs
  - GET  /api/runs      -> status of launched runs + DB apply status
  - GET  /api/job-statuses -> recent DB rows with apply status
  - GET  /api/log?path=<log_path>&tail=<N> -> tail N lines of a log file
  - GET  /api/health    -> liveness probe
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
APP_DB = Path.home() / ".autojob" / "autojob.db"
APPLY_LOG_DIR = Path.home() / ".autojob" / "logs"
APPLYPILOT_SRC = REPO_ROOT / "job_automation_rc" / "src"

# The venv ships both `python` and `python3`; prefer `python3` for macOS compat.
_VENV_BIN = REPO_ROOT / "job_automation_rc" / "venv" / "bin"
APPLYPILOT_VENV_PYTHON = _VENV_BIN / "python3"
if not APPLYPILOT_VENV_PYTHON.exists():
    # Fallback to bare `python` if the distro only creates that name.
    APPLYPILOT_VENV_PYTHON = _VENV_BIN / "python"

DEFAULT_RESUME_PDF = REPO_ROOT / "Ramnarayan_CV (9).pdf"
DEFAULT_RESUME_TXT = REPO_ROOT / "Ramnarayan_CV (9).txt"

# Maximum lines returned by the /api/log tail endpoint.
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_MAX = 2000


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class RunEntry:
    url: str
    job_id: int | None
    company: str
    position: str
    pid: int
    started_at: int
    log_path: str
    command: list[str]
    process: subprocess.Popen[str] = field(repr=False)
    stdout_handle: Any = field(repr=False, default=None)
    state: str = "running"
    finished_at: int | None = None
    returncode: int | None = None


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------
class DashboardState:
    def __init__(self) -> None:
        self.runs: dict[str, RunEntry] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers (must be called with self.lock held)
    # ------------------------------------------------------------------
    def _refresh_locked(self) -> None:
        for run in self.runs.values():
            if run.state != "running":
                continue
            rc = run.process.poll()
            if rc is None:
                continue
            run.state = "finished"
            run.returncode = rc
            run.finished_at = int(time.time())
            self._close_log_handle(run)

    @staticmethod
    def _close_log_handle(run: RunEntry) -> None:
        if run.stdout_handle:
            try:
                run.stdout_handle.flush()
                run.stdout_handle.close()
            except Exception:
                pass
            run.stdout_handle = None

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str], grace_seconds: float = 5.0) -> int | None:
        """SIGINT -> SIGTERM -> SIGKILL escalation with a grace period."""
        if proc.poll() is not None:
            return proc.poll()

        # Attempt graceful stop first.
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass

        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            rc = proc.poll()
            if rc is not None:
                return rc
            time.sleep(0.1)

        # Escalate to SIGTERM.
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            return proc.wait(timeout=2)
        except Exception:
            pass

        # Last resort: SIGKILL.
        try:
            proc.kill()
        except Exception:
            pass
        return proc.poll()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start_run(self, run: RunEntry) -> tuple[bool, str]:
        with self.lock:
            self._refresh_locked()
            existing = self.runs.get(run.url)
            if existing and existing.state == "running":
                return False, "Apply is already running for this job URL"
            self.runs[run.url] = run
            return True, "started"

    def payload(self) -> list[dict[str, Any]]:
        with self.lock:
            self._refresh_locked()
            rows: list[dict[str, Any]] = []
            for run in self.runs.values():
                db = lookup_db_status(run.url)
                rows.append(
                    {
                        "url": run.url,
                        "job_id": run.job_id,
                        "company": run.company,
                        "position": run.position,
                        "state": run.state,
                        "pid": run.pid,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "returncode": run.returncode,
                        "log_path": run.log_path,
                        "db_apply_status": db.get("apply_status", ""),
                        "db_apply_error": db.get("apply_error", ""),
                        "db_applied_at": db.get("applied_at", ""),
                    }
                )
            rows.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
            return rows

    def stop_run(self, url: str) -> tuple[bool, str]:
        with self.lock:
            self._refresh_locked()
            run = self.runs.get(url)
            if not run:
                return False, "Run not found for URL"
            if run.state != "running":
                return False, f"Run is already {run.state}"
            rc = self._stop_process(run.process)
            run.state = "stopped"
            run.returncode = rc if rc is not None else -15
            run.finished_at = int(time.time())
            self._close_log_handle(run)
            return True, "stopped"

    def stop_run_by_pid(self, pid: int) -> tuple[bool, str]:
        with self.lock:
            self._refresh_locked()
            for run in self.runs.values():
                if run.pid != pid:
                    continue
                if run.state != "running":
                    return False, f"Run is already {run.state}"
                rc = self._stop_process(run.process)
                run.state = "stopped"
                run.returncode = rc if rc is not None else -15
                run.finished_at = int(time.time())
                self._close_log_handle(run)
                return True, "stopped"
            return False, "Run not found for PID"

    def stop_all(self) -> int:
        with self.lock:
            self._refresh_locked()
            stopped = 0
            for run in self.runs.values():
                if run.state != "running":
                    continue
                rc = self._stop_process(run.process)
                run.state = "stopped"
                run.returncode = rc if rc is not None else -15
                run.finished_at = int(time.time())
                self._close_log_handle(run)
                stopped += 1
            return stopped

    def shutdown(self) -> None:
        """Terminate all child processes and close all file handles."""
        with self.lock:
            self._refresh_locked()
            for run in self.runs.values():
                if run.process.poll() is None:
                    try:
                        run.process.terminate()
                    except Exception:
                        pass
                self._close_log_handle(run)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def slugify(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in value)
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return (cleaned[:70] or "job").lower()


def _safe_int(value: Any) -> int | None:
    """Convert value to int, returning None for any non-integer-like input."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def lookup_db_status(url: str) -> dict[str, str]:
    if not APP_DB.exists():
        return {"apply_status": "", "apply_error": "", "applied_at": ""}
    try:
        with sqlite3.connect(str(APP_DB)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT apply_status, apply_error, applied_at
                FROM jobs
                WHERE url = ? OR application_url = ?
                LIMIT 1
                """,
                (url, url),
            ).fetchone()
    except sqlite3.Error as exc:
        log.debug("DB lookup error for %s: %s", url, exc)
        return {"apply_status": "", "apply_error": "", "applied_at": ""}
    if not row:
        return {"apply_status": "", "apply_error": "", "applied_at": ""}
    return {
        "apply_status": row["apply_status"] or "",
        "apply_error": row["apply_error"] or "",
        "applied_at": row["applied_at"] or "",
    }


def list_db_status_rows(limit: int = 5000) -> list[dict[str, str]]:
    if not APP_DB.exists():
        return []
    try:
        with sqlite3.connect(str(APP_DB)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    url,
                    application_url,
                    apply_status,
                    apply_error,
                    applied_at,
                    last_attempted_at,
                    verification_confidence
                FROM jobs
                WHERE apply_status IS NOT NULL OR applied_at IS NOT NULL
                ORDER BY COALESCE(last_attempted_at, applied_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error as exc:
        log.warning("DB list error: %s", exc)
        return []
    return [
        {
            "url": row["url"] or "",
            "application_url": row["application_url"] or "",
            "apply_status": row["apply_status"] or "",
            "apply_error": row["apply_error"] or "",
            "applied_at": row["applied_at"] or "",
            "last_attempted_at": row["last_attempted_at"] or "",
            "verification_confidence": row["verification_confidence"] or "",
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Apply-pipeline Python resolver
# ---------------------------------------------------------------------------
def build_env() -> dict[str, str]:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    src = str(APPLYPILOT_SRC)
    if existing_pp:
        if src not in existing_pp.split(os.pathsep):
            env["PYTHONPATH"] = src + os.pathsep + existing_pp
    else:
        env["PYTHONPATH"] = src

    if "APPLYPILOT_RESUME_PDF" not in env and DEFAULT_RESUME_PDF.exists():
        env["APPLYPILOT_RESUME_PDF"] = str(DEFAULT_RESUME_PDF)
    if "APPLYPILOT_RESUME_TXT" not in env and DEFAULT_RESUME_TXT.exists():
        env["APPLYPILOT_RESUME_TXT"] = str(DEFAULT_RESUME_TXT)
    return env


def resolve_apply_python(explicit_python: str) -> str:
    """Return the first executable Python interpreter that exists on disk."""
    candidates: list[str] = []
    if explicit_python:
        candidates.append(explicit_python)

    env_python = os.environ.get("APPLYPILOT_PYTHON", "").strip()
    if env_python:
        candidates.append(env_python)

    # Prefer the project venv; fall back to system python3 / sys.executable.
    candidates.append(str(APPLYPILOT_VENV_PYTHON))
    python3_on_path = shutil.which("python3")
    if python3_on_path:
        candidates.append(python3_on_path)
    candidates.append(sys.executable)

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return sys.executable


def can_run_apply_python(python_bin: str, env: dict[str, str]) -> tuple[bool, str]:
    """Verify that python_bin can import the autojob CLI."""
    try:
        result = subprocess.run(
            [python_bin, "-c", "import typer; import autojob.cli"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    return False, err[:400]


def choose_apply_python(preferred: str, env: dict[str, str]) -> tuple[str, str]:
    """Return (interpreter_path, error_message). error_message is empty on success."""
    candidates: list[str] = []
    seen: set[str] = set()
    # Prefer venv python3, then system python3, then sys.executable as last resort.
    fallbacks = [preferred, str(APPLYPILOT_VENV_PYTHON)]
    python3_on_path = shutil.which("python3")
    if python3_on_path:
        fallbacks.append(python3_on_path)
    fallbacks.append(sys.executable)

    for raw in fallbacks:
        if not raw:
            continue
        path = str(Path(raw).expanduser())
        if path in seen:
            continue
        seen.add(path)
        if not Path(path).exists() or not os.access(path, os.X_OK):
            continue
        candidates.append(path)

    last_error = "No usable Python interpreter found."
    for candidate in candidates:
        ok, error = can_run_apply_python(candidate, env)
        if ok:
            return candidate, ""
        if error:
            last_error = f"{candidate}: {error}"

    # Fall back gracefully even when preflight fails — the real error will appear
    # in the apply log instead of blocking the request.
    return candidates[0] if candidates else sys.executable, last_error


def build_cmd(url: str, args: argparse.Namespace, python_bin: str) -> list[str]:
    cmd = [
        python_bin,
        "-m",
        "autojob.cli",
        "apply",
        "--limit",
        "1",
        "--url",
        url,
        "--model",
        args.model,
        "--min-score",
        str(args.min_score),
    ]
    if args.headless:
        cmd.append("--headless")
    if args.use_main_profile:
        cmd.append("--use-main-profile")
    if args.chrome_profile:
        cmd.extend(["--chrome-profile", args.chrome_profile])
    return cmd


def is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Log-tail helper
# ---------------------------------------------------------------------------
def tail_file(path: Path, n: int) -> list[str]:
    """Return the last *n* lines of *path* without loading the whole file."""
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            # Use a small deque-like approach for large files.
            lines: list[str] = []
            for line in fh:
                lines.append(line.rstrip("\n"))
                if len(lines) > n:
                    lines.pop(0)
        return lines
    except OSError:
        return []


# ---------------------------------------------------------------------------
# HTTP handler factory
# ---------------------------------------------------------------------------
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def create_handler(args: argparse.Namespace, state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        # ------------------------------------------------------------------
        # Response helpers
        # ------------------------------------------------------------------
        def _send_cors(self) -> None:
            for k, v in _CORS_HEADERS.items():
                self.send_header(k, v)

        def _json(self, status_code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self._json(404, {"ok": False, "error": "Not found"})
                return
            content = path.read_bytes()
            _mime = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
            }
            ctype = _mime.get(path.suffix, "application/octet-stream")
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        # Suppress per-request HTTP log noise (use our own logger for errors).
        def log_message(self, fmt: str, *args_) -> None:  # noqa: A003
            pass

        def log_error(self, fmt: str, *args_) -> None:
            log.error(fmt, *args_)

        # ------------------------------------------------------------------
        # CORS preflight
        # ------------------------------------------------------------------
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._send_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ------------------------------------------------------------------
        # GET
        # ------------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            if path in {"/", "/index.html"}:
                self._serve_file(DASHBOARD_DIR / "index.html")
                return
            if path == "/styles.css":
                self._serve_file(DASHBOARD_DIR / "styles.css")
                return
            if path == "/app.js":
                self._serve_file(DASHBOARD_DIR / "app.js")
                return
            if path == "/jobs.json":
                source = DASHBOARD_DIR / "jobs.json"
                if not source.exists():
                    source = REPO_ROOT / "jobs.json"
                self._serve_file(source)
                return
            if path == "/application_log.json":
                source = DASHBOARD_DIR / "application_log.json"
                if not source.exists():
                    source = REPO_ROOT / "application_log.json"
                self._serve_file(source)
                return
            if path == "/api/runs":
                self._json(200, {"ok": True, "runs": state.payload()})
                return
            if path == "/api/job-statuses":
                self._json(200, {"ok": True, "statuses": list_db_status_rows()})
                return
            if path == "/api/health":
                self._json(200, {"ok": True, "time": int(time.time())})
                return
            if path == "/api/log":
                qs = parse_qs(parsed.query)
                log_path_raw = (qs.get("path") or [""])[0].strip()
                tail_n = min(
                    _safe_int((qs.get("tail") or [str(_LOG_TAIL_DEFAULT)])[0]) or _LOG_TAIL_DEFAULT,
                    _LOG_TAIL_MAX,
                )
                if not log_path_raw:
                    self._json(400, {"ok": False, "error": "Missing ?path= parameter"})
                    return
                log_path_obj = Path(log_path_raw).resolve()
                # Safety: only allow reading files under the log directory.
                try:
                    log_path_obj.relative_to(APPLY_LOG_DIR.resolve())
                except ValueError:
                    self._json(403, {"ok": False, "error": "Access denied"})
                    return
                lines = tail_file(log_path_obj, tail_n)
                self._json(200, {"ok": True, "path": str(log_path_obj), "lines": lines})
                return

            self._json(404, {"ok": False, "error": "Not found"})

        # ------------------------------------------------------------------
        # POST
        # ------------------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in {"/api/apply", "/api/stop", "/api/stop-all"}:
                self._json(404, {"ok": False, "error": "Not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "Invalid JSON payload"})
                return

            # ---- /api/stop-all ----------------------------------------
            if path == "/api/stop-all":
                stopped = state.stop_all()
                self._json(
                    200,
                    {"ok": True, "message": f"stopped {stopped} run(s)", "runs": state.payload()},
                )
                return

            # ---- /api/stop --------------------------------------------
            if path == "/api/stop":
                url = str(payload.get("url") or "").strip()
                pid_raw = payload.get("pid")
                pid_int = _safe_int(pid_raw)

                ok = False
                message = "Run not found"
                if pid_int is not None:
                    ok, message = state.stop_run_by_pid(pid_int)
                elif url:
                    ok, message = state.stop_run(url)
                else:
                    self._json(400, {"ok": False, "error": "Provide url or pid"})
                    return

                code = 200 if ok else 409
                self._json(code, {"ok": ok, "message": message, "runs": state.payload()})
                return

            # ---- /api/apply -------------------------------------------
            url = str(payload.get("url") or "").strip()
            if not is_valid_http_url(url):
                self._json(400, {"ok": False, "error": "Invalid or missing URL"})
                return

            job_id = _safe_int(payload.get("job_id"))
            company = str(payload.get("company") or "")
            position = str(payload.get("position") or "")

            APPLY_LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            log_path = APPLY_LOG_DIR / f"dashboard_apply_{ts}_{slugify(url)}.log"

            try:
                out = log_path.open("w", encoding="utf-8")
            except OSError as exc:
                self._json(500, {"ok": False, "error": f"Cannot open log file: {exc}"})
                return

            env = build_env()
            apply_python, python_error = choose_apply_python(args.apply_python, env)
            if python_error:
                log.warning("Interpreter preflight warning: %s", python_error)
                out.write(f"Interpreter preflight warning: {python_error}\n")

            cmd = build_cmd(url, args, apply_python)
            out.write(f"Command: {' '.join(cmd)}\n")
            out.flush()

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(REPO_ROOT),
                    env=env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except OSError as exc:
                out.write(f"Failed to launch process: {exc}\n")
                out.close()
                self._json(500, {"ok": False, "error": f"Failed to launch process: {exc}"})
                return

            run = RunEntry(
                url=url,
                job_id=job_id,
                company=company,
                position=position,
                pid=proc.pid,
                started_at=int(time.time()),
                log_path=str(log_path),
                command=cmd,
                process=proc,
                stdout_handle=out,
            )
            ok, message = state.start_run(run)
            if not ok:
                proc.terminate()
                try:
                    out.close()
                except Exception:
                    pass
                self._json(409, {"ok": False, "error": message, "runs": state.payload()})
                return

            log.info("Apply started: pid=%d url=%s", proc.pid, url)
            self._json(200, {"ok": True, "message": message, "pid": proc.pid, "runs": state.payload()})

    return Handler


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dashboard UI + Apply API")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument("--model", default="gemini-2.5-flash", help="LLM model for apply pipeline")
    parser.add_argument("--min-score", type=int, default=7, help="Minimum match score to apply")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--use-main-profile", action="store_true", help="Use main Chrome profile")
    parser.add_argument("--chrome-profile", default="", help="Chrome profile directory")
    parser.add_argument(
        "--apply-python",
        default="",
        help="Python interpreter for the apply pipeline (defaults to project venv python3).",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open browser tab")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()
    args.apply_python = resolve_apply_python(args.apply_python)
    state = DashboardState()
    handler = create_handler(args, state)

    # SO_REUSEADDR lets us restart quickly without "Address already in use".
    ThreadingHTTPServer.allow_reuse_address = True

    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        log.error("Cannot bind %s:%d — %s", args.host, args.port, exc)
        sys.exit(1)

    url = f"http://{args.host}:{args.port}"
    log.info("Dashboard server running at %s", url)
    log.info("Apply interpreter: %s", args.apply_python)
    log.info("Press Ctrl+C to stop")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down…")
    finally:
        server.server_close()
        state.shutdown()
        log.info("Goodbye.")


if __name__ == "__main__":
    main()

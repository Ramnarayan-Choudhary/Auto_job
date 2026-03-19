#!/usr/bin/env python3
"""Local dashboard server with one-click Apply API.

Serves the existing dashboard UI and adds:
  - POST /api/apply  -> launch one ApplyPilot run for a specific URL
  - POST /api/stop   -> stop one running ApplyPilot run
  - GET  /api/runs   -> status of launched runs + DB apply status
"""

from __future__ import annotations

import argparse
import json
import os
import signal
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
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
APP_DB = Path.home() / ".applypilot" / "applypilot.db"
APPLY_LOG_DIR = Path.home() / ".applypilot" / "logs"
APPLYPILOT_SRC = REPO_ROOT / "ApplyPilot_isolated_test" / "src"
APPLYPILOT_VENV_PYTHON = REPO_ROOT / "ApplyPilot_isolated_test" / "venv" / "bin" / "python"
DEFAULT_RESUME_PDF = REPO_ROOT / "Ramnarayan_CV (9).pdf"
DEFAULT_RESUME_TXT = REPO_ROOT / "Ramnarayan_CV (9).txt"


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


class DashboardState:
    def __init__(self) -> None:
        self.runs: dict[str, RunEntry] = {}
        self.lock = threading.Lock()

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
            if run.stdout_handle:
                try:
                    run.stdout_handle.flush()
                    run.stdout_handle.close()
                except Exception:
                    pass
                run.stdout_handle = None

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
        if proc.poll() is not None:
            return proc.poll()
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

        try:
            proc.terminate()
        except Exception:
            pass
        try:
            return proc.wait(timeout=2)
        except Exception:
            pass

        try:
            proc.kill()
        except Exception:
            pass
        return proc.poll()

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
        with self.lock:
            self._refresh_locked()
            for run in self.runs.values():
                if run.process.poll() is None:
                    run.process.terminate()


def slugify(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in value)
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return (cleaned[:70] or "job").lower()


def lookup_db_status(url: str) -> dict[str, str]:
    if not APP_DB.exists():
        return {"apply_status": "", "apply_error": "", "applied_at": ""}
    try:
        conn = sqlite3.connect(str(APP_DB))
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
        conn.close()
    except Exception:
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
        conn = sqlite3.connect(str(APP_DB))
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
        conn.close()
    except Exception:
        return []
    payload: list[dict[str, str]] = []
    for row in rows:
        payload.append(
            {
                "url": row["url"] or "",
                "application_url": row["application_url"] or "",
                "apply_status": row["apply_status"] or "",
                "apply_error": row["apply_error"] or "",
                "applied_at": row["applied_at"] or "",
                "last_attempted_at": row["last_attempted_at"] or "",
                "verification_confidence": row["verification_confidence"] or "",
            }
        )
    return payload


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    src = str(APPLYPILOT_SRC)
    if existing_pp:
        if src not in existing_pp.split(os.pathsep):
            env["PYTHONPATH"] = src + os.pathsep + existing_pp
    else:
        env["PYTHONPATH"] = src

    # Set defaults for resume paths if not already exported by user.
    if "APPLYPILOT_RESUME_PDF" not in env and DEFAULT_RESUME_PDF.exists():
        env["APPLYPILOT_RESUME_PDF"] = str(DEFAULT_RESUME_PDF)
    if "APPLYPILOT_RESUME_TXT" not in env and DEFAULT_RESUME_TXT.exists():
        env["APPLYPILOT_RESUME_TXT"] = str(DEFAULT_RESUME_TXT)
    return env


def resolve_apply_python(explicit_python: str) -> str:
    candidates: list[str] = []
    if explicit_python:
        candidates.append(explicit_python)
    env_python = os.environ.get("APPLYPILOT_PYTHON", "").strip()
    if env_python:
        candidates.append(env_python)
    candidates.append(str(APPLYPILOT_VENV_PYTHON))
    candidates.append(sys.executable)

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return sys.executable


def can_run_apply_python(python_bin: str, env: dict[str, str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                python_bin,
                "-c",
                "import typer; import applypilot.cli",
            ],
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
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in [preferred, str(APPLYPILOT_VENV_PYTHON), sys.executable]:
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

    return preferred or sys.executable, last_error


def build_cmd(url: str, args: argparse.Namespace, python_bin: str) -> list[str]:
    cmd = [
        python_bin,
        "-m",
        "applypilot.cli",
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


def create_handler(args: argparse.Namespace, state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status_code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
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
            if path.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            elif path.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif path.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif path.suffix == ".json":
                ctype = "application/json; charset=utf-8"
            else:
                ctype = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, fmt: str, *args_) -> None:  # noqa: A003
            del fmt, args_

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
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
                self._json(200, {"ok": True})
                return
            self._json(404, {"ok": False, "error": "Not found"})

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

            if path == "/api/stop-all":
                stopped = state.stop_all()
                self._json(200, {"ok": True, "message": f"stopped {stopped} run(s)", "runs": state.payload()})
                return

            if path == "/api/stop":
                url = str(payload.get("url") or "").strip()
                pid_raw = payload.get("pid")
                ok = False
                message = "Run not found"
                if isinstance(pid_raw, int):
                    ok, message = state.stop_run_by_pid(pid_raw)
                elif str(pid_raw).isdigit():
                    ok, message = state.stop_run_by_pid(int(pid_raw))
                elif url:
                    ok, message = state.stop_run(url)
                else:
                    self._json(400, {"ok": False, "error": "Provide url or pid"})
                    return

                code = 200 if ok else 409
                self._json(code, {"ok": ok, "message": message, "runs": state.payload()})
                return

            url = str(payload.get("url") or "").strip()
            if not is_valid_http_url(url):
                self._json(400, {"ok": False, "error": "Invalid URL"})
                return

            job_id = payload.get("job_id")
            company = str(payload.get("company") or "")
            position = str(payload.get("position") or "")

            APPLY_LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            log_path = APPLY_LOG_DIR / f"dashboard_apply_{ts}_{slugify(url)}.log"
            out = log_path.open("w", encoding="utf-8")
            env = build_env()
            apply_python, python_error = choose_apply_python(args.apply_python, env)
            if python_error:
                out.write(f"Interpreter preflight warning: {python_error}\n")
            cmd = build_cmd(url, args, apply_python)
            out.write(f"Command: {' '.join(cmd)}\n")
            out.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
            )

            run = RunEntry(
                url=url,
                job_id=int(job_id) if str(job_id).isdigit() else None,
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

            self._json(200, {"ok": True, "message": message, "runs": state.payload()})

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dashboard UI + Apply API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--min-score", type=int, default=7)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--use-main-profile", action="store_true")
    parser.add_argument("--chrome-profile", default="")
    parser.add_argument(
        "--apply-python",
        default="",
        help="Python interpreter used for apply pipeline (defaults to ApplyPilot venv).",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open browser")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.apply_python = resolve_apply_python(args.apply_python)
    state = DashboardState()
    handler = create_handler(args, state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard server running at {url}")
    print(f"Apply interpreter: {args.apply_python}")
    print("Press Ctrl+C to stop")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.shutdown()


if __name__ == "__main__":
    main()

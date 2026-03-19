# ApplyPilot Autonomous Final Repo

Production-ready snapshot of your current autonomous job-apply system:

- `ApplyPilot_isolated_test/`: main ApplyPilot pipeline + Gemini browser apply worker
- `dashboard/`: localhost UI (job list, View, Apply, Stop, live status)
- `parse_jobs.py`, `jobs.json`, `application_log.json`: job ingest + data
- `2026-AI-College-Jobs/`: source markdown job lists
- `auto_apply.py`: legacy flow retained for reference

## 1) Setup

Requirements:

- Python `>=3.11`
- Google Chrome installed

Install:

```bash
cd ApplyPilot_isolated_test
python3 -m venv venv
source venv/bin/activate
pip install -e .
playwright install chrome
cd ..
```

## 2) Configure

Create local env file (do not commit):

```bash
cp ApplyPilot_isolated_test/.env.example .env
```

Set at minimum:

```bash
GEMINI_API_KEY=your_key_here
```

Optional runtime controls:

```bash
APPLYPILOT_AGENT_MAX_STEPS=70
APPLYPILOT_MANUAL_LOGIN_TIMEOUT=50
APPLYPILOT_AUTO_SIGNUP_FALLBACK=1
```

## 3) Run Dashboard + Apply API

```bash
python3 dashboard/server.py --use-main-profile --chrome-profile "Profile 2"
```

Open: `http://localhost:8080`

- Click `View` to inspect job
- Click `Apply` to launch one-job pipeline
- Click `Stop` to stop a running pipeline
- Status updates come from backend DB + run API

## 4) Run CLI Directly (Optional)

```bash
PYTHONPATH=ApplyPilot_isolated_test/src ApplyPilot_isolated_test/venv/bin/python -m applypilot.cli apply --limit 1 --url "<job_url>" --use-main-profile --chrome-profile "Profile 2"
```

## 5) Push to GitHub

```bash
git init
git add .
git commit -m "Final autonomous ApplyPilot repo"
git branch -M main
git remote add origin <your_repo_url>
git push -u origin main
```

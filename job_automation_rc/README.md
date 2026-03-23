# AutoJob
**Developed by Ramnarayan Choudhary**

Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.

[![PyPI version](https://img.shields.io/pypi/v/autojob?color=blue)](https://pypi.org/project/autojob/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Ramnarayan-Choudhary/job_automation_rc?style=social)](https://github.com/Ramnarayan-Choudhary/job_automation_rc)

---

## 🚀 What is AutoJob?

I built **AutoJob** because applying to jobs manually is broken and tedious. AutoJob is a 6-stage autonomous job application pipeline that I designed to automate the entire job hunt. 

It discovers jobs across 5+ major boards, uses AI to score them against your resume, tailors your resume for each specific job, writes matching cover letters, and **submits the applications for you autonomously**. It effortlessly navigates complex forms, uploads documents, bypassing CAPTCHAs, and answers screening questions hands-free.

---

## ⚡ Installation

AutoJob requires Python 3.11+ and Google Chrome installed on your machine.
Run the following commands to install the system:

```bash
# 1. Install the main AutoJob package
pip install autojob

# 2. Install the web-scraping dependencies (job spy)
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
```

---

## 🛠 Quick Start

Three commands are all you need to start automating your job hunt:

```bash
# 1. Run the one-time interactive setup (configure your resume, profile, and Gemini API keys)
autojob init

# 2. Run the discovery and preparation pipeline (scrapes boards, scores jobs, and tailors resumes)
autojob run

# 3. Launch the autonomous browser worker to submit the prepared applications!
autojob apply
```

#### Parallel Processing for Speed
Want to move faster? You can run multiple workers simultaneously:
```bash
autojob run -w 4      # 4 threads for job discovery & enrichment
autojob apply -w 3    # 3 autonomous Chrome instances applying at the same time
```

---

## 🧠 How The Pipeline Works

I architected AutoJob into 6 distinct, highly-optimized stages:

| Stage | What It Does |
|-------|-------------|
| **1. Discover** | Scrapes 5 massive job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites. |
| **2. Enrich** | Visits each job URL and extracts the full description using a powerful 3-tier cascade: JSON-LD, CSS selectors, or AI-powered extraction. |
| **3. Score** | An AI agent rates every job 1-10 based strictly on your resume and preferences. Only the best jobs proceed. |
| **4. Tailor** | AI rewrites your resume per job—emphasizing your most relevant experience and injecting job description keywords. It *never* fabricates metrics. |
| **5. Cover Letter** | Generates a targeted, context-aware cover letter for the specific company and role. |
| **6. Auto-Apply** | A Gemini-powered browser worker launches Chrome, fills the forms, uploads your tailored documents, answers demographic/screening questions, and confidently clicks Submit. |

---

## ⚙️ Configuration Files

After running `autojob init`, the system will generate three critical files in your `~/.autojob/` directory:

1. **`profile.json`**: Your master data file. Contains your contact info, demographics (Gender/Race/Disability), compensation expectations, and skills. AutoJob uses this to confidently auto-fill massive application forms.
2. **`searches.yaml`**: Your job search queries. You can define multiple boards, target titles, and locations.
3. **`.env`**: Stores your API keys. **Gemini is highly recommended and completely free** via Google AI Studio. You can optionally add a `CAPSOLVER_API_KEY` to automatically bypass invisible reCAPTCHAs!

---

## 💻 Full Command Reference

```bash
autojob init                         # First-time setup wizard
autojob doctor                       # Verify setup, diagnose missing requirements
autojob run [stages...]              # Run pipeline stages (or 'all')
autojob run --workers 4              # Parallel discovery/enrichment
autojob run --stream                 # Concurrent stages (streaming mode)
autojob run --min-score 8            # Override score threshold
autojob run --dry-run                # Preview without executing
autojob apply                        # Launch auto-apply
autojob apply --workers 3            # Parallel browser workers
autojob apply --dry-run              # Fill forms without submitting
autojob apply --continuous           # Run forever, polling for new jobs
autojob apply --headless             # Headless browser mode
autojob apply --url URL              # Apply to a specific job
autojob dashboard                    # Open the Local UI to apply manually
autojob status                       # View pipeline statistics & success rates
```

---

## 🤝 Contributing & License

AutoJob is an open-source tool built to level the playing field for job seekers. Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for how you can help improve the system.

AutoJob is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0). You are free to use, modify, and distribute this software, but any modified version used as a service must also be open-sourced under the same license.

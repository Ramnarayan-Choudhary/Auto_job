# Contributing to AutoJob

Thank you for your interest in contributing to **AutoJob**! I (Ramnarayan Choudhary) created this project to revolutionize and automate the tedious job application process, and open-source contributions are highly encouraged to make the system even more robust against complex ATS platforms.

This guide covers everything you need to get started.

---

## First-time Setup

Because AutoJob uses browser-use and Playwright to interact with complex DOM structures, you need to install it in editable mode with development dependencies.

1. **Clone your fork**:
   ```bash
   git clone https://github.com/Ramnarayan-Choudhary/job_automation_rc.git
   cd job_automation_rc
   ```

2. **Create a virtual environment** (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. **Install in editable mode**:
   ```bash
   pip install -e ".[dev]"
   playwright install chromium
   ```

This installs AutoJob in editable mode with all development dependencies (pytest, ruff) and downloads the Chromium browser binary for Playwright.

---

## Development Workflow

AutoJob uses `Ruff` for linting and formatting, and `pytest` for unit testing.

1. **Create a branch**:
   Always create a new branch for your feature or bug fix:
   ```bash
   git checkout -b feature/my-new-feature
   ```

2. **Make your changes**:
   Try to keep changes focused and well-scoped. If you are adding a new ATS module or employer portal, please refer to the `src/autojob/discovery/workday.py` for structural inspiration.

3. **Format and Lint**:
   Ensure your code matches the project style:
   ```bash
   ruff check .
   ruff format .
   ```

4. **Run Tests**:
   Run the test suite to ensure nothing is broken:
   ```bash
   pytest
   ```

5. **Commit and Push**:
   Write clear commit messages, explaining *why* the change was made if it isn't obvious.
   ```bash
   git commit -m "Fix: handled hidden combobox on SmartRecruiters"
   git push origin feature/my-new-feature
   ```

6. **Open a Pull Request**:
   Submit your PR against the `main` branch. I try to review PRs quickly!

---

## Code of Conduct

Please treat everyone with respect. AutoJob is entirely open-source, and all contributions are valued equally. 

By contributing to AutoJob, you agree that your contributions will be licensed under the [GNU Affero General Public License v3.0](LICENSE).

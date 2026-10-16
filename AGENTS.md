# Repository Guidelines

## Project Structure & Module Organization

`gaybar_tools/` contains the Python application: `browser.py` adapts webpages, `workflow.py` coordinates learning pages, `llm.py` handles model requests and answer validation, and `ui.py`/`worker.py` connect Tkinter to browser automation. Configuration, cookies, paths, and filesystem isolation have separate modules.

`gaybar_tools/demo/` holds packaged HTML, JavaScript, CSS, and media fixtures. `tests/` contains unit and browser integration tests. Read [docs/HOW_AGENT_TO_BUILD.md](docs/HOW_AGENT_TO_BUILD.md) for requirements and [docs/VIDEO_NOTES.md](docs/VIDEO_NOTES.md) for validation boundaries.

## Build, Test, and Development Commands

Run from the repository root:

- `python3 scripts/bootstrap.py`: create `.venv` and install locked dependencies; add `--browser` to download project-local Chromium.
- `./run.sh`: launch the desktop application.
- `./run.sh --doctor`: check the environment.
- `./run.sh --demo-headless`: run the account-free local demonstration.
- `.venv/bin/python -B -m unittest discover -s tests -v`: run all tests.
- `python3 -B -c 'from setuptools.build_meta import build_wheel; build_wheel("dist")'`: build a wheel using an interpreter with setuptools 77+.

Windows equivalents are `py scripts\bootstrap.py`, `run.bat`, and `.venv\Scripts\python.exe`.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for functions/modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Follow existing type hints and dataclasses. No formatter or linter is configured; match surrounding code.

Keep synchronous Playwright objects on their owning worker thread and communicate with Tkinter through queues. Preserve cancellation checks, natural video playback, and sequential task processing.

## Testing Guidelines

Use standard-library `unittest`, files named `test_*.py`, and methods named `test_*`. Add focused regressions for behavioral changes, using local fixtures and synthetic model responses. Browser tests need Chromium and, on Linux, working bubblewrap. Report skipped checks; no numeric coverage threshold is configured.

Target one module with `discover -s tests -p test_next_warning_integration.py -v`. Distinguish local fixture results from real-site verification.

## Commit & Pull Request Guidelines

Recent commits use concise Chinese descriptions, often prefixed `From Codex：`. Keep titles focused on the resulting change. PRs should describe the problem, behavior, validation, and limitations; link relevant issues and include redacted screenshots for UI changes.

## Configuration & Environment Safety

Keep generated files within the project or `/tmp`, dependencies in `.venv`, and runtime data in `.runtime`. Preserve isolated browser profiles and Linux write confinement. Never commit keys, cookies, profiles, or account-bearing diagnostics. Use saved credentials and external APIs only within existing user authorization.

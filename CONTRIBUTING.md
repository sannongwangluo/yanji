# Contributing

Thanks for considering contributing to YanJi (言纪)! We welcome bug reports, feature requests, and pull requests.

*You may write issues and PRs in English or Chinese — 可以用中文提交 issue 和 PR。*

## Before you start

- Search the existing [issues](../../issues) to avoid duplicates.
- For new features, open an issue first to agree on the approach before writing code.

## Development setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
```

Run the tests:

```bash
python -m unittest discover -s tests
```

## Pull requests

- Keep changes small and focused — one PR, one thing.
- Make sure the full test suite passes before opening a PR: `python -m unittest discover -s tests` (54 tests, all green).
- Follow the existing code style.
- Use English commit messages in conventional commits style (e.g. `feat:`, `fix:`, `docs:`).

## Issues

Use the bug report or feature request template when opening an issue. You may write in Chinese.

Thanks for helping make YanJi better!

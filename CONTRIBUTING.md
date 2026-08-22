# Contributing to Code2Database

Thank you for your interest in contributing! This guide covers how to set up the development environment and submit changes.

## Development Setup

```bash
# Clone and install dependencies
git clone https://github.com/your-org/Code2Database.git
cd Code2Database
bash scripts/setup.sh

# Or manually:
pip install -r scripts/requirements.txt
pip install pytest  # for running tests
```

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Making Changes

1. **Fork** the repository and create a feature branch
2. **Make your changes** — keep diffs minimal and focused
3. **Run tests** — ensure `python3 -m pytest tests/` passes
4. **Update documentation** — if you add features, update `docs/en/` and `docs/zh/` accordingly
5. **Submit a pull request** with a clear description of the change

## Code Style

- Python: Follow PEP 8; 4-space indentation
- Keep functions focused and single-purpose
- Use type hints for public APIs
- No `__pycache__` or `.pyc` files in commits (gitignored)

## Documentation

- Documentation lives in `docs/en/` (English) and `docs/zh/` (Chinese)
- The `SKILL.md` in each language directory is the main skill instruction file
- When adding new features, update both language versions
- Reference docs go in `docs/*/references/`

## Adding a New Language Scanner

1. Create `scripts/_scanner/<lang>_scanner.py` inheriting from `base.py`
2. Implement `scan_file()` to extract functions, edges, and callbacks
3. Register in `scripts/_scanner/__init__.py`
4. Add tree-sitter binding to `scripts/requirements.txt`
5. Add tests in `tests/`
6. Update `skill.json` and both `SKILL.md` files

## Adding a New Capability Module

When extending Code2Database with a new reasoning, query, or operational capability:

1. Implement the module in `scripts/_builder/<module>.py`
2. Wire CLI commands in `scripts/code2database_builder.py` (argparse routing)
3. Add unit tests in `tests/` covering the core behavior
4. Document config fields (if any) in `docs/<lang>/RUNTIME_CONFIG.md` (both EN and ZH)
5. Update `skill.json` `commands` array and `output_files` (if new artifacts are produced)
6. Update `docs/<lang>/SKILL.md` Quick Reference and Constraint sections (both EN and ZH)
7. Update `README.md` and `docs/zh/README.md` Capability/Feature tables
8. Update `CLAUDE.md` and `AGENTS.md` if the capability introduces constraints or query entry points
9. Add a CHANGELOG entry under `### Added` describing the capability and its commands

Keep the boundary clear: SKILL.md is for AI agents using the tool; CLAUDE.md/AGENTS.md are for developers modifying the tool; reference docs are for on-demand detail; OVERVIEW.md is internal architecture only.

## Reporting Issues

- Use GitHub Issues
- Include: OS, Python version, reproduction steps, expected vs actual behavior
- For scan quality issues, include a small code sample that demonstrates the problem

# Project notes for Claude

## Documentation maintenance (important)

**Keep the documentation in sync with the code on every major change.** A "major
change" means anything that alters the project's structure, behaviour, or
interface — e.g. adding/removing/renaming a module, changing the CLI or a flag,
changing the routing algorithm or an output format, adding a dependency, or
changing a design rule / invariant.

When you make such a change, update all three in the same piece of work:

1. **`README.md`** — user-facing: install, CLI options, examples, limitations.
2. **`docs/architecture.md`** — developer-facing: module roles, data flow,
   algorithms, and the DRC-clean invariants.
3. **API docs** — regenerate from the docstrings:
   ```bash
   pip install -e ".[docs]"      # pdoc, into the tf venv
   pdoc -d google --mermaid pyautoroute -o docs/api
   ```
   (Also update the relevant module/function docstrings themselves, since the API
   docs are generated from them. Docstrings use **Google style** — an `Args:`
   block documents each parameter, `Returns:` the result — so `-d google` renders
   them. `--mermaid` draws the architecture diagram, which is included on the
   package landing page via the `.. include:: ../docs/architecture.md` directive
   in `pyautoroute/__init__.py`, so `docs/architecture.md` is the single source.)

Treat docs as part of "done": a change isn't complete until the docs reflect it.

## Versioning

The version lives in `pyproject.toml` (`[project].version`). We follow SemVer,
adapted for a pre-1.0 project:

- **Minor** bump (`0.3.0` → `0.4.0`) for each *major addition*: a new feature,
  CLI flag, output, or routing-algorithm change (the same "major change" bar the
  docs rule uses).
- **Patch** bump (`0.4.0` → `0.4.1`) for bug fixes and small corrections.
- **Docs-only** changes need no bump (fold them into the next one).
- Stay pre-1.0 until the CLI / file-output interface is declared stable; while
  `0.x`, even breaking changes only bump the minor. Reserve `1.0.0` for the
  stability commitment.

Bump in the same piece of work as the change (it's part of "done", like the
docs). Tagging a release is optional: `git tag v0.4.0`.

On every version bump, add a matching entry to **`CHANGES.md`** (newest first) — a
one-or-two-line human-readable summary of the change. It's part of "done" too.

## Environment

- Python lives in the **`tf` venv**: `/Users/que/venvs/tf` (Python 3.12, with
  numpy / scipy / shapely / matplotlib / pytest).
- Run tests from the repo root: `pytest` (or `/Users/que/venvs/tf/bin/python -m pytest`).
- `pcbnew` is intentionally **not** used — the board is parsed from the
  `.kicad_pcb` s-expression directly.
- Generated routing output (`*_routed.kicad_pcb`, `*_routed.png`) is gitignored;
  do not commit it.

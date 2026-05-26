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
   pdoc pyautoroute -o docs/api
   ```
   (Also update the relevant module/function docstrings themselves, since the API
   docs are generated from them.)

Treat docs as part of "done": a change isn't complete until the docs reflect it.

## Environment

- Python lives in the **`tf` venv**: `/Users/que/venvs/tf` (Python 3.12, with
  numpy / scipy / shapely / matplotlib / pytest).
- Run tests from the repo root: `pytest` (or `/Users/que/venvs/tf/bin/python -m pytest`).
- `pcbnew` is intentionally **not** used — the board is parsed from the
  `.kicad_pcb` s-expression directly.
- Generated routing output (`*_routed.kicad_pcb`, `*_routed.png`) is gitignored;
  do not commit it.

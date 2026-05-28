#!/usr/bin/env bash
#
# PyAutoRoute helper menu — common project tasks in one place.
#
#   ./pyautoroute.sh           # interactive menu
#
# Each action echoes the exact command it runs, so this script also serves as a
# cheat-sheet. Override the interpreter with PYTHON=/path/to/python.

set -u
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

run() { echo "+ $*"; "$@"; }
pause() { echo; read -rp "Press Enter to return to the menu... " _ || true; }

# Pick a board under TestProjects/ and echo its path on stdout (prompts on stderr).
pick_board() {
    local boards=(TestProjects/*/*.kicad_pcb) i=1 choice
    {
        echo "Boards:"
        for b in "${boards[@]}"; do echo "  $i) $b"; i=$((i + 1)); done
    } >&2
    read -rp "board number: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#boards[@]} )); then
        echo "${boards[$((choice - 1))]}"
    fi
}

install_pkg() {
    echo "Extras: 1) dev+viz+docs (all)  2) dev only  3) runtime only"
    read -rp "choice [1]: " e
    case "${e:-1}" in
        2) run "$PYTHON" -m pip install -e ".[dev]" ;;
        3) run "$PYTHON" -m pip install -e "." ;;
        *) run "$PYTHON" -m pip install -e ".[dev,viz,docs]" ;;
    esac
}

update_docs() {
    run "$PYTHON" -m pip install -e ".[docs]"
    run pdoc -d google --mermaid pyautoroute -o docs/api
    echo "API docs regenerated in docs/api/."
}

run_tests() {
    echo "Suite: 1) short (pytest)  2) long (pytest --slow)"
    read -rp "choice [1]: " t
    case "${t:-1}" in
        2) run "$PYTHON" -m pytest --slow ;;
        *) run "$PYTHON" -m pytest ;;
    esac
}

route_board() {
    local board; board="$(pick_board)"
    [[ -z "$board" ]] && { echo "no board selected"; return; }
    read -rp "place footprints first? [y/N]: " place
    local args=(--time 30 --debug-plot)
    [[ "$place" =~ ^[Yy] ]] && args=(--place --place-time 20 "${args[@]}")
    run "$PYTHON" -m pyautoroute.autoroute "$board" "${args[@]}"
}

write_settings() {
    local board; board="$(pick_board)"
    [[ -z "$board" ]] && { echo "no board selected"; return; }
    run "$PYTHON" -m pyautoroute.autoroute "$board" --write-config
}

tune_settings() {
    local board; board="$(pick_board)"
    [[ -z "$board" ]] && { echo "no board selected"; return; }
    read -rp "seconds per probed setting [5]: " t
    run "$PYTHON" -m pyautoroute.tune "$board" --time "${t:-5}"
}

clean_outputs() {
    echo "Generated files to remove:"
    find . \( -name '*_routed.kicad_pcb' -o -name '*_placed.kicad_pcb' \
        -o -name '*_placed_routed.kicad_pcb' -o -name '*_routed.png' \
        -o -name '*_placed.png' -o -name '*_placed_routed.png' \
        -o -name '*.pyautoroute.cfg' -o -name '*.log' \) -not -path './.git/*' -print
    find . -type d -name snapshots -not -path './.git/*' -print
    read -rp "delete these? [y/N]: " yes
    [[ "$yes" =~ ^[Yy] ]] || { echo "skipped"; return; }
    find . \( -name '*_routed.kicad_pcb' -o -name '*_placed.kicad_pcb' \
        -o -name '*_placed_routed.kicad_pcb' -o -name '*_routed.png' \
        -o -name '*_placed.png' -o -name '*_placed_routed.png' \
        -o -name '*.pyautoroute.cfg' -o -name '*.log' \) -not -path './.git/*' -delete
    find . -type d -name snapshots -not -path './.git/*' -exec rm -rf {} +
    echo "cleaned."
}

menu() {
    cat <<'EOF'

PyAutoRoute — tasks
  1) Install the package (pip install -e)
  2) Update API docs from the code (pdoc)
  3) Run tests (short or long)
  4) Route a test board
  5) Write a settings file for a board
  6) Find good settings for a board (parameter sweep)
  7) Clean generated outputs
  8) Quit
EOF
    read -rp "choice: " c || return 1      # EOF (piped/empty input) -> quit
    case "$c" in
        1) install_pkg ;;
        2) update_docs ;;
        3) run_tests ;;
        4) route_board ;;
        5) write_settings ;;
        6) tune_settings ;;
        7) clean_outputs ;;
        8|q|Q) return 1 ;;
        *) echo "unknown choice: $c" ;;
    esac
    return 0
}

while menu; do pause; done

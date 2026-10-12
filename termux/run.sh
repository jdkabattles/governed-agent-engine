#!/data/data/com.termux/files/usr/bin/bash
# Run the agent from the repo checkout. On non-Termux systems: `bash termux/run.sh ...`.
# Default: one cycle (`run --once`). Pass any gae subcommand to override, e.g. `run`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
export GAE_ROOT="${GAE_ROOT:-$HERE}"

PY=".venv/bin/python"
[[ -e "$PY" ]] || { echo "No .venv found; run termux/install.sh first" >&2; exit 1; }

if [[ $# -eq 0 ]]; then set -- run --once; fi
exec "$PY" -m governed_agent_engine "$@"

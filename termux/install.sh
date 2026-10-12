#!/data/data/com.termux/files/usr/bin/bash
# One-command install for Termux (Android, aarch64/arm/x86_64) and plain Linux.
#
#   Public repo:
#     curl -fsSL https://raw.githubusercontent.com/jdkabattles/governed-agent-engine/main/termux/install.sh | bash
#   Private repo (token is used for this run only and never written to disk):
#     export GITHUB_TOKEN=<fine-grained read-only token>
#     curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" \
#       https://raw.githubusercontent.com/jdkabattles/governed-agent-engine/main/termux/install.sh | bash
#
# Env overrides: GAE_DIR (install dir), GAE_REPO_URL (git URL or local path), GAE_BRANCH,
#                GAE_FETCH=tarball (skip git; download the GitHub tarball),
#                GAE_RUN_ONCE=1 (run one agent cycle after install).
# Non-Termux: run with `bash install.sh` (the shebang targets Termux).
set -euo pipefail

REPO_URL="${GAE_REPO_URL:-https://github.com/jdkabattles/governed-agent-engine.git}"
BRANCH="${GAE_BRANCH:-main}"
DIR="${GAE_DIR:-$HOME/governed-agent-engine}"

say() { printf '\033[1;36m[gae]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[gae] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

IS_TERMUX=0
if [[ -n "${PREFIX:-}" && "$PREFIX" == *com.termux* ]]; then IS_TERMUX=1; fi

# 1) System packages. Only prebuilt Termux packages: python + git. No Rust, no compiler needed.
if [[ $IS_TERMUX -eq 1 ]]; then
  say "Termux detected ($(uname -m)); installing python + git via pkg"
  pkg install -y python git curl >/dev/null || die "pkg install failed (try: termux-change-repo)"
  BIN_DIR="$PREFIX/bin"
  LAUNCH_SHEBANG="#!$PREFIX/bin/bash"   # absolute: works even without termux-exec
else
  command -v git >/dev/null || die "git is required"
  BIN_DIR="$HOME/.local/bin"
  LAUNCH_SHEBANG="#!/usr/bin/env bash"
fi
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python3 not found"
"$PY" - <<'PYEOF' || die "Python >= 3.11 required"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYEOF

# 2) Fetch or update the repo. A token, if set, goes in a per-command header only.
#    Fallback: if git fails (or GAE_FETCH=tarball), download the branch tarball instead.
fetch_tarball() {
  local slug api
  slug="${REPO_URL#https://github.com/}"; slug="${slug%.git}"
  api="https://api.github.com/repos/$slug/tarball/$BRANCH"
  local hdr=()
  [[ -n "${GITHUB_TOKEN:-}" ]] && hdr=(-H "Authorization: Bearer $GITHUB_TOKEN")
  say "Downloading tarball of $slug@$BRANCH"
  mkdir -p "$DIR"
  curl -fsSL "${hdr[@]}" "$api" | tar -xz --strip-components=1 -C "$DIR" \
    || die "tarball download failed (private repo? set GITHUB_TOKEN)"
}
git_auth=()
if [[ -n "${GITHUB_TOKEN:-}" && "$REPO_URL" == https://github.com/* ]]; then
  basic="$(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')"
  git_auth=(-c "http.https://github.com/.extraheader=Authorization: Basic $basic")
fi
if [[ "${GAE_FETCH:-git}" == "tarball" ]]; then
  fetch_tarball
elif [[ -d "$DIR/.git" ]]; then
  say "Updating $DIR"
  git "${git_auth[@]}" -C "$DIR" pull --ff-only origin "$BRANCH" || die "git pull failed"
else
  say "Cloning into $DIR"
  if ! git "${git_auth[@]}" clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"; then
    rm -rf "$DIR"
    [[ "$REPO_URL" == https://github.com/* ]] || die "git clone failed"
    say "git clone failed; falling back to tarball download"
    fetch_tarball
  fi
fi
cd "$DIR"

# 3) Virtualenv + package. Single runtime dep: PyYAML (pure-Python fallback, no compiler).
if [[ ! -e .venv/bin/python ]]; then
  say "Creating virtualenv"
  "$PY" -m venv .venv
fi
say "Installing package (stdlib core + PyYAML)"
.venv/bin/python -m pip install --disable-pip-version-check -q -e . \
  || die "pip install failed"

# 4) Launcher on PATH.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/gae" <<LAUNCH
$LAUNCH_SHEBANG
export GAE_ROOT="\${GAE_ROOT:-$DIR}"
exec "$DIR/.venv/bin/python" -m governed_agent_engine "\$@"
LAUNCH
chmod +x "$BIN_DIR/gae"

# 5) Self-check.
"$BIN_DIR/gae" validate >/dev/null || die "config validation failed"
say "Installed. Try:  gae status   |  gae run --once   |  gae smoke"
if [[ "${GAE_RUN_ONCE:-0}" == "1" ]]; then
  "$BIN_DIR/gae" run --once
fi

#!/usr/bin/env bash
# Refresh the graphify knowledge graph for this repo.
#
# Default: AST-only refresh (free, ~3 seconds). Picks up code changes since
# the last build. Same operation the post-commit hook runs.
#
# `--semantic` flag: also re-run Gemini semantic extraction over docs/configs.
# Costs LLM tokens (~$0.07 last time) — use only when docs change meaningfully.
#
# Reads GEMINI_API_KEY from the platform's .env (gitignored) so you don't
# have to paste it. Falls back to the existing shell env if .env is missing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Source .env if present so GEMINI_API_KEY (and friends) become available
# without polluting the calling shell. `set -a` exports every var; `set +a`
# restores normal scoping.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

case "${1:-}" in
  --semantic|-s)
    if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
      echo "ERROR: GEMINI_API_KEY (or GOOGLE_API_KEY) is not set." >&2
      echo "Add it to .env or export it before running --semantic." >&2
      exit 1
    fi
    echo "[refresh_graph] Semantic refresh — Gemini will run on docs/configs (~\$0.07 typical)."
    SCRIPT="$REPO_ROOT/scripts/graphify_semantic_build.py"
    if [ ! -f "$SCRIPT" ]; then
      echo "ERROR: $SCRIPT not found." >&2
      exit 1
    fi
    # graphify is installed via `uv tool install`, which puts the import
    # path inside its own isolated env. Use that interpreter so the script
    # can `import graphify`. PYTHON env var overrides if set.
    GRAPHIFY_PY_DEFAULT="$HOME/.local/share/uv/tools/graphifyy/bin/python"
    PY="${PYTHON:-$GRAPHIFY_PY_DEFAULT}"
    if [ ! -x "$PY" ]; then
      echo "ERROR: graphify python interpreter not found at $PY" >&2
      echo "(Set PYTHON= to override, or reinstall: uv tool install --force 'graphifyy[gemini]')" >&2
      exit 1
    fi
    "$PY" "$SCRIPT" "$REPO_ROOT"
    ;;
  ""|--ast|-a)
    echo "[refresh_graph] AST-only refresh (free)."
    graphify update .
    ;;
  -h|--help)
    cat <<EOF
Usage: scripts/refresh_graph.sh [--semantic|--ast]

  (default)        AST-only refresh — free, ~3 seconds. Picks up code changes.
  --semantic, -s   Re-run Gemini semantic extraction over docs/configs (paid).
  --ast, -a        Same as default — explicit.
  -h, --help       Show this help.

The post-commit git hook already runs AST refresh after every commit, so
manually invoking the AST mode is rarely needed. Run --semantic only when
PENDING.md / SOW docs / carrier YAMLs change meaningfully.
EOF
    ;;
  *)
    echo "Unknown flag: $1" >&2
    echo "Run 'scripts/refresh_graph.sh --help' for usage." >&2
    exit 2
    ;;
esac

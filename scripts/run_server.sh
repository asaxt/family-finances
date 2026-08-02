#!/bin/zsh

set -u
umask 077

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$project_dir/.local.env" ]]; then
  source "$project_dir/.local.env"
fi
export FAMILY_FINANCES_DATA_DIR="${FAMILY_FINANCES_DATA_DIR:-$project_dir}"
export FAMILY_FINANCES_PORT="${FAMILY_FINANCES_PORT:-4242}"
export FAMILY_FINANCES_MODE="${FAMILY_FINANCES_MODE:-stable}"
export FAMILY_FINANCES_DISABLE_PLAID="${FAMILY_FINANCES_DISABLE_PLAID:-0}"

cd "$project_dir" || exit 1
exec "$project_dir/.venv/bin/python" app.py

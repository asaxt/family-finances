#!/bin/zsh

set -u
umask 077

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$project_dir/.local.env" ]]; then
  source "$project_dir/.local.env"
fi
export FAMILY_FINANCES_DATA_DIR="${FAMILY_FINANCES_DATA_DIR:-$project_dir}"

cd "$project_dir" || exit 1
exec "$project_dir/.venv/bin/python" app.py

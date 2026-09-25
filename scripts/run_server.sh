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

if [[ "${FAMILY_FINANCES_READ_ONLY_MIRROR:-0}" == "1" ]]; then
  if [[ "$FAMILY_FINANCES_MODE" != "development" || ! -f "${FAMILY_FINANCES_MIRROR_PREPARE_SCRIPT:-}" ]]; then
    print -u2 "The production mirror requires the configured development snapshot helper."
    exit 1
  fi
  mirror_snapshot_dir="$("$project_dir/.venv/bin/python" "$FAMILY_FINANCES_MIRROR_PREPARE_SCRIPT")" || exit 1
  export FAMILY_FINANCES_DATA_DIR="$mirror_snapshot_dir"
  export FAMILY_FINANCES_READ_ONLY_MIRROR=1
  export FAMILY_FINANCES_DISABLE_PLAID=1
fi

cd "$project_dir" || exit 1
exec "$project_dir/.venv/bin/python" app.py

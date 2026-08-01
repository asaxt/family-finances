#!/bin/zsh

set -u
umask 077

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_url="http://127.0.0.1:4242"
log_file="$HOME/Library/Logs/Family Finances.log"

if [[ -f "$project_dir/.local.env" ]]; then
  source "$project_dir/.local.env"
fi
export FAMILY_FINANCES_DATA_DIR="${FAMILY_FINANCES_DATA_DIR:-$project_dir}"

if ! /usr/bin/curl -fsS "$app_url/health" >/dev/null 2>&1; then
  cd "$project_dir" || exit 1
  /usr/bin/nohup "$project_dir/.venv/bin/python" app.py >> "$log_file" 2>&1 &

  for _ in {1..40}; do
    if /usr/bin/curl -fsS "$app_url/health" >/dev/null 2>&1; then
      break
    fi
    /bin/sleep 0.25
  done
fi

if /usr/bin/curl -fsS "$app_url/health" >/dev/null 2>&1; then
  /usr/bin/open "$app_url"
else
  /usr/bin/osascript -e 'display alert "Family Finances could not start" message "Check ~/Library/Logs/Family Finances.log for details." as critical'
fi

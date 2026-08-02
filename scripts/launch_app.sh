#!/bin/zsh

set -u
umask 077

project_dir="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -f "$project_dir/.local.env" ]]; then
  source "$project_dir/.local.env"
fi
export FAMILY_FINANCES_DATA_DIR="${FAMILY_FINANCES_DATA_DIR:-$project_dir}"
export FAMILY_FINANCES_PORT="${FAMILY_FINANCES_PORT:-4242}"
launch_label="${FAMILY_FINANCES_LAUNCH_LABEL:-local.family-finances}"
log_name="${FAMILY_FINANCES_LOG_NAME:-Family Finances}"
app_url="http://127.0.0.1:$FAMILY_FINANCES_PORT"
log_file="$HOME/Library/Logs/$log_name.log"

if ! /usr/bin/curl -fsS "$app_url/health" >/dev/null 2>&1; then
  /bin/launchctl remove "$launch_label" >/dev/null 2>&1 || true
  /bin/launchctl submit \
    -l "$launch_label" \
    -o "$log_file" \
    -e "$log_file" \
    -- "$project_dir/scripts/run_server.sh"

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

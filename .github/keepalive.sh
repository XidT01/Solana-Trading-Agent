#!/usr/bin/env bash
# Restart another scan workflow if it has no run in progress or waiting.
# Each scan loop calls this for the other one after every cycle, so the Solana and EVM scanners keep
# each other alive even when GitHub skips scheduled (cron) runs.
# Usage: .github/keepalive.sh <workflow file>   (needs GH_TOKEN with actions: write)
wf="$1"

# Stop restarting once the campaign is over.
if ! python -c "import sys; from agent.common import load_config, utcnow; sys.exit(utcnow().date().isoformat() > load_config()['campaign']['end_date'])"; then
  exit 0
fi

active=$(gh run list --workflow "$wf" --limit 20 --json status \
  --jq '[.[] | select(.status != "completed")] | length' 2>/dev/null) || {
  echo "::warning::keepalive could not check $wf"
  exit 0
}
if [ "$active" = "0" ]; then
  echo "::notice::$wf was not running; starting it"
  gh workflow run "$wf" --ref "$GITHUB_REF_NAME" || echo "::warning::could not start $wf"
fi

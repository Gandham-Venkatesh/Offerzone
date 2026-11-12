#!/usr/bin/env bash
# How scanning runs take turns on GitHub Actions, so that one of them is always on duty.
#
#   relay.sh wait    before scanning: wait until no older scanning run is at work
#   relay.sh shift   scan for one shift; with KEEP_RUNNING=1, start the next run shortly before the
#                    end and stay on duty until that run has a machine and stands waiting
#
# Why: a run used to start its successor and leave. On 11 Nov 2026 GitHub accepted such a
# successor, kept it queued for 14 minutes and then failed to start it. Nothing was alive to
# notice, and the monitor was dark for four and a half hours. Now the run on duty leaves only
# once its relief is really there; until then it simply keeps scanning, and it starts another
# relief if the first one never comes up.
#
# Uses the gh command with the run's own token. Settings, all from the environment:
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_REF_NAME, GITHUB_EVENT_NAME   set by GitHub
#   KEEP_RUNNING   "1": each run starts the next one
#   LOOP_SECONDS   length of a shift in seconds (default 840)
#   RELAY_HOPS     for tests: how many more runs this chain may start (empty: no limit)
#   DRY_RUN        non-empty: scan without pushing (tests)
set -u

REPO="${GITHUB_REPOSITORY:?}"
ME="${GITHUB_RUN_ID:?}"
BRANCH="${GITHUB_REF_NAME:?}"
WORKFLOW="${RELAY_WORKFLOW:-monitor.yml}"
POLL="${RELAY_POLL:-5}"
WAIT_STEP="Wait for my turn"   # the names of two steps in the workflow
SCAN_STEP="Scan"

say() { echo "relay: $*"; }

# One line per scanning run on this branch: "id status age-in-minutes". Checks (doctor, test
# push) are not scans. Fails when GitHub cannot be asked, so that "nobody there" and "no
# answer" stay two different things.
runs() {
  gh api "repos/$REPO/actions/workflows/$WORKFLOW/runs?branch=$BRANCH&per_page=40" --jq '
    .workflow_runs[]
    | select(((.display_title // "") | startswith("Loot check")) | not)
    | "\(.id) \(.status) \(((now - (.created_at | fromdateiso8601)) / 60) | floor)"'
}

surplus() {  # leave without scanning; the workflow skips the remaining steps
  say "$1"
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "surplus=1" >> "$GITHUB_OUTPUT"
  exit 0
}

wait_turn() {
  local waited=0 limit="${RELAY_WAIT_LIMIT:-1500}" list failures=0 at_work queued fresh
  while :; do
    if ! list=$(runs 2>/dev/null); then
      failures=$((failures + 1))
      if [ "$failures" -ge 6 ]; then say "GitHub does not answer; starting without knowing who else runs"; return 0; fi
      sleep "$POLL"; continue
    fi
    failures=0
    at_work=$(echo "$list" | awk -v me="$ME" '$1 + 0 < me + 0 && $2 == "in_progress" { print $1 }')
    if [ "$(echo "$at_work" | grep -c .)" -ge 2 ]; then
      surplus "one run is scanning and another already waits to take over; this one is not needed"
    fi
    if [ "${KEEP_RUNNING:-}" = "1" ] && [ "${GITHUB_EVENT_NAME:-}" = "schedule" ] && [ "$waited" -eq 0 ]; then
      # The runs start each other; the schedule is only there to restart them after a break.
      fresh=$(echo "$list" | awk -v me="$ME" '$1 + 0 < me + 0 && $2 == "in_progress" && $3 + 0 < 60 { print $1 }')
      [ -n "$fresh" ] && surplus "the monitor is running (run $(echo "$fresh" | head -1)); nothing to restart"
    fi
    if [ -z "$at_work" ]; then
      # Older runs that never got a machine must not wake up later and scan beside this one.
      queued=$(echo "$list" | awk -v me="$ME" '$1 + 0 < me + 0 && $2 != "in_progress" && $2 != "completed" { print $1 }')
      for run in $queued; do
        say "cancelling run $run, which was started earlier and never got going"
        gh run cancel "$run" --repo "$REPO" >/dev/null 2>&1 || true
      done
      [ "$waited" -gt 0 ] && say "my turn after ${waited}s"
      return 0
    fi
    if [ "$waited" -ge "$limit" ]; then
      say "run $(echo "$at_work" | head -1) has still not finished after ${waited}s; taking over"
      return 0
    fi
    [ "$waited" -eq 0 ] && say "run $(echo "$at_work" | head -1) is on duty; waiting for it to finish"
    sleep "$POLL"; waited=$((waited + POLL))
  done
}

# Is that run standing ready (or already scanning)? Then it has a machine and a working install.
at_its_post() {
  local state
  state=$(gh api "repos/$REPO/actions/runs/$1/jobs" --jq "
    [.jobs[0].steps[]? | select((.name == \"$WAIT_STEP\" or .name == \"$SCAN_STEP\") and .status == \"in_progress\")] | length" 2>/dev/null) || return 1
  [ "${state:-0}" -ge 1 ]
}

start_next() {
  local args=(-f mode=run)
  [ -n "${RELAY_HOPS:-}" ] && args+=(-f "hops=$((RELAY_HOPS - 1))")
  [ -n "${DRY_RUN:-}" ] && args+=(-f dry_run=1)
  [ -n "${SHIFT_SECONDS_INPUT:-}" ] && args+=(-f "shift_seconds=$SHIFT_SECONDS_INPUT")
  [ -n "${WATCHLIST_FILE:-}" ] && args+=(-f "watchlist_file=$WATCHLIST_FILE")
  [ -n "${CHANNELS_FILE:-}" ] && args+=(-f "channels_file=$CHANNELS_FILE")
  [ -n "${FEEDS_FILE:-}" ] && args+=(-f "feeds_file=$FEEDS_FILE")
  gh workflow run "$WORKFLOW" --repo "$REPO" --ref "$BRANCH" "${args[@]}" >/dev/null 2>&1
}

# Runs beside the scanner. Shortly before the shift ends it gets a relief going, and it creates
# the file the scanner is waiting for once that relief stands ready.
find_relief() {
  local flag="$1" shift_seconds="${LOOP_SECONDS:-840}" lead="${RELAY_LEAD:-45}" slept=0
  local started=0 last_start=0 failed=0 now list younger deadline ready
  while [ "$slept" -lt $((shift_seconds - lead)) ]; do sleep "$POLL"; slept=$((slept + POLL)); done
  deadline=$(( $(date +%s) + lead + ${RELAY_MAX_OVERTIME:-1500} ))
  while :; do
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      say "no next run came up in time; leaving it to the schedule"; echo none > "$flag"; return 0
    fi
    if list=$(runs 2>/dev/null); then
      younger=$(echo "$list" | awk -v me="$ME" '$1 + 0 > me + 0 && $2 != "completed" { print $1 }' | sort -n)
      ready=""
      for run in $younger; do
        if at_its_post "$run"; then ready="$run"; break; fi
      done
      if [ -n "$ready" ]; then
        say "run $ready is up and waiting: handing over"; echo "$ready" > "$flag"; return 0
      fi
      # Start one when there is none, and another when the last one has not come up for three
      # minutes (a run that sits in GitHub's queue may never start).
      if [ -z "$younger" ] || [ $((now - last_start)) -ge "${RELAY_PATIENCE:-180}" ]; then
        if [ "$started" -ge "${RELAY_MAX_STARTS:-5}" ]; then
          if [ -z "$younger" ]; then
            say "started $started runs and none came up; leaving it to the schedule"; echo none > "$flag"; return 0
          fi
        elif start_next; then
          started=$((started + 1)); last_start=$now; failed=0
          say "started the next run (attempt $started)"
        else
          failed=$((failed + 1))
          say "GitHub refused to start the next run (attempt $failed)"
          if [ "$failed" -ge 3 ]; then
            say "giving up: is the workflow disabled? The schedule will restart the monitor if it is not."
            echo none > "$flag"; return 0
          fi
        fi
      fi
    else
      say "GitHub does not answer; staying on duty"
    fi
    sleep "$POLL"
  done
}

run_shift() {
  local flag="${RUNNER_TEMP:-/tmp}/relief-ready-$ME" finder="" code=0
  local args=(--max-runtime "${LOOP_SECONDS:-840}")
  [ -n "${DRY_RUN:-}" ] && args+=(--dry-run)
  rm -f "$flag"
  if [ "${KEEP_RUNNING:-}" != "1" ]; then
    say "KEEP_RUNNING is not 1: this run does not start a next one"
  elif [ -n "${RELAY_HOPS:-}" ] && [ "$RELAY_HOPS" -le 0 ]; then
    say "last run of this test chain"
  else
    find_relief "$flag" &
    finder=$!
    args+=(--handover-file "$flag" --max-overtime "${RELAY_MAX_OVERTIME:-1500}")
  fi
  # The scanner needs no GitHub token; only this script talks to GitHub.
  env -u GH_TOKEN -u GITHUB_TOKEN python -m lootwatch "${args[@]}" || code=$?
  if [ -n "$finder" ]; then
    kill "$finder" 2>/dev/null
    wait "$finder" 2>/dev/null
  fi
  return "$code"
}

case "${1:-}" in
  wait) wait_turn ;;
  shift) run_shift ;;
  *) echo "usage: relay.sh wait|shift" >&2; exit 2 ;;
esac

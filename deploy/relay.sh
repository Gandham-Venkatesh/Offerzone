#!/usr/bin/env bash
# How scanning runs take turns on GitHub Actions, so that one of them is always on duty.
#
#   relay.sh check   is this run needed at all? (a scheduled run is not, while the runs keep
#                    each other going; a third run is not, while one scans and one waits)
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
# The three commands are three steps of the workflow, and their names matter: a run counts as
# "at its post" only while its "Wait for my turn" or "Scan" step is in progress. A run that is
# still deciding whether it is needed is in neither, so nobody ever hands over to a run that is
# about to leave.
#
# Uses the gh command with the run's own token. Settings, all from the environment:
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_REF_NAME, GITHUB_EVENT_NAME   set by GitHub
#   KEEP_RUNNING   "1": each run starts the next one
#   LOOP_SECONDS   length of a shift in seconds (default 840)
#   RELAY_HOPS     test branches only: how many more runs this chain may start (empty: no limit)
#   DRY_RUN        non-empty: scan without pushing (set by the workflow on every branch but main)
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

# Is that run standing ready or scanning? Then it has a machine, a working install, and has
# decided to stay.
at_its_post() {
  local state
  state=$(gh api "repos/$REPO/actions/runs/$1/jobs" --jq "
    [.jobs[0].steps[]? | select((.name == \"$WAIT_STEP\" or .name == \"$SCAN_STEP\") and .status == \"in_progress\")] | length" 2>/dev/null) || return 1
  [ "${state:-0}" -ge 1 ]
}

older_at_work() {  # ids of older scanning runs that have a machine, from a runs listing
  echo "$1" | awk -v me="$ME" '$1 + 0 < me + 0 && $2 == "in_progress" { print $1 }'
}

surplus() {  # leave without scanning; the workflow skips the remaining steps
  say "$1"
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "surplus=1" >> "$GITHUB_OUTPUT"
  exit 0
}

# Step "Am I needed?". Decided once, before this run takes its place in the queue.
check_needed() {
  local list="" tries=0 on_schedule=0 fresh posted=0
  [ "${KEEP_RUNNING:-}" = "1" ] && [ "${GITHUB_EVENT_NAME:-}" = "schedule" ] && on_schedule=1
  until list=$(runs 2>/dev/null); do
    tries=$((tries + 1))
    if [ "$tries" -ge 6 ]; then
      # Not knowing who else runs, a scheduled run stays out of the way: its only job is to
      # restart the runs after a break, and the next one will look again in a few minutes.
      [ "$on_schedule" = 1 ] && surplus "GitHub does not answer, so it is unknown whether the monitor is running; leaving it be"
      say "GitHub does not answer; going on to wait for my turn"
      return 0
    fi
    sleep "$POLL"
  done
  if [ "$on_schedule" = 1 ]; then
    # The runs start each other; the schedule is only there to restart them after a break.
    fresh=$(echo "$list" | awk -v me="$ME" '$1 + 0 < me + 0 && $2 == "in_progress" && $3 + 0 < 60 { print $1 }' | head -1)
    [ -n "$fresh" ] && surplus "the monitor is running (run $fresh); nothing to restart"
  fi
  for run in $(older_at_work "$list"); do
    at_its_post "$run" && posted=$((posted + 1))
  done
  [ "$posted" -ge 2 ] && surplus "one run is scanning and another already waits to take over; this one is not needed"
  return 0
}

# Step "Wait for my turn". From here on this run stays: it scans as soon as nobody older does.
wait_turn() {
  local waited=0 limit="${RELAY_WAIT_LIMIT:-1500}" list failures=0 at_work queued
  while :; do
    if ! list=$(runs 2>/dev/null); then
      failures=$((failures + 1))
      # Starting blind means two runs may scan side by side for a shift; waiting means nobody may.
      # Five minutes without an answer, then the second risk is the bigger one.
      if [ $((failures * POLL)) -ge "${RELAY_BLIND_AFTER:-300}" ]; then
        say "GitHub has not answered for $((failures * POLL))s; starting without knowing who else runs"
        return 0
      fi
      sleep "$POLL"; waited=$((waited + POLL)); continue
    fi
    failures=0
    at_work=$(older_at_work "$list")
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

start_next() {
  local args=(-f mode=run)
  [ -n "${RELAY_HOPS:-}" ] && args+=(-f "hops=$((RELAY_HOPS - 1))")
  [ -n "${SHIFT_SECONDS_INPUT:-}" ] && args+=(-f "shift_seconds=$SHIFT_SECONDS_INPUT")
  [ -n "${WATCHLIST_FILE:-}" ] && args+=(-f "watchlist_file=$WATCHLIST_FILE")
  [ -n "${CHANNELS_FILE:-}" ] && args+=(-f "channels_file=$CHANNELS_FILE")
  [ -n "${FEEDS_FILE:-}" ] && args+=(-f "feeds_file=$FEEDS_FILE")
  gh workflow run "$WORKFLOW" --repo "$REPO" --ref "$BRANCH" "${args[@]}" >/dev/null 2>&1
}

# Runs beside the scanner. Shortly before the shift ends it gets a relief going. It creates the
# file the scanner is waiting for once a relief stands ready, and takes the file away again if
# that relief disappears before the scanner has left. It never gives up before the scanner's
# overtime is used up: a refusal from GitHub is a reason to keep scanning, not to stop.
find_relief() {
  local flag="$1" shift_seconds="${LOOP_SECONDS:-840}" lead="${RELAY_LEAD:-45}" slept=0
  local started=0 failed=0 next_start=0 now list younger ready="" began deadline pause
  local most="${RELAY_MAX_STARTS:-8}" lag="${RELAY_LISTING_LAG:-30}"
  while [ "$slept" -lt $((shift_seconds - lead)) ]; do sleep "$POLL"; slept=$((slept + POLL)); done
  began=$(date +%s)
  deadline=$((began + lead + ${RELAY_MAX_OVERTIME:-1500}))
  while :; do
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      say "no next run came up in time; leaving it to the schedule"; echo none > "$flag"; return 0
    fi
    if list=$(runs 2>/dev/null); then
      if [ -n "$ready" ]; then
        # The scanner may need most of a minute to finish its scan and leave. Until it has, keep
        # an eye on the relief it was promised.
        if ! echo "$list" | awk -v id="$ready" '$1 == id && $2 != "completed" { found = 1 } END { exit !found }'; then
          say "run $ready is gone before it could take over; looking for another"
          rm -f "$flag"; ready=""
        fi
      fi
      if [ -z "$ready" ]; then
        younger=$(echo "$list" | awk -v me="$ME" '$1 + 0 > me + 0 && $2 != "completed" { print $1 " " $2 }' | sort -n)
        for run in $(echo "$younger" | awk '$2 == "in_progress" { print $1 }'); do
          if at_its_post "$run"; then ready="$run"; break; fi
        done
        if [ -n "$ready" ]; then
          say "run $ready is up and waiting: handing over"; echo "$ready" > "$flag"
        elif [ "$now" -ge "$next_start" ]; then
          # Start one when there is none, and another when the last one has not come up for three
          # minutes (a run that sits in GitHub's queue may never start). A run just started gets
          # half a minute to show up in the list before it is taken for missing.
          if [ "$started" -ge "$most" ]; then
            [ "$started" -eq "$most" ] && say "started $started runs and none came up; scanning on and waiting"
            started=$((most + 1))
          elif start_next; then
            started=$((started + 1)); failed=0
            next_start=$((now + ${RELAY_PATIENCE:-180}))
            say "started the next run (attempt $started)"
          else
            failed=$((failed + 1))
            pause=$((30 * failed)); [ "$pause" -gt 300 ] && pause=300
            next_start=$((now + pause))
            say "GitHub refused to start the next run (refusal $failed); staying on duty, next try in ${pause}s"
          fi
        elif [ -z "$younger" ] && [ "$failed" -eq 0 ] && [ "$started" -gt 0 ] && [ $((next_start - now)) -lt $((${RELAY_PATIENCE:-180} - lag)) ]; then
          # The run that was started half a minute ago is not in the list (any more): it never
          # came to be, or it has already ended. Do not sit out the three minutes for it.
          next_start=$now
        fi
      fi
    else
      say "GitHub does not answer; staying on duty"
    fi
    # Quick looks while a relief is expected any second, slower ones during a long wait: the
    # run's token is allowed about a thousand questions an hour.
    if [ -z "$ready" ] && [ $((now - began)) -gt 180 ]; then sleep $((POLL * 4)); else sleep "$POLL"; fi
  done
}

run_shift() {
  local flag="${RUNNER_TEMP:-/tmp}/relief-ready-$ME" finder="" code=0 rc=0 restarts=0 elapsed remaining
  local shift_seconds="${LOOP_SECONDS:-840}" overtime="${RELAY_MAX_OVERTIME:-1500}" began
  local args=(--max-runtime "$shift_seconds")
  [ -n "${DRY_RUN:-}" ] && args+=(--dry-run)
  rm -f "$flag"
  if [ "${KEEP_RUNNING:-}" != "1" ]; then
    say "KEEP_RUNNING is not 1: this run does not start a next one"
  elif [ -n "${RELAY_HOPS:-}" ] && [ "$RELAY_HOPS" -le 0 ]; then
    say "last run of this test chain"
  else
    find_relief "$flag" &
    finder=$!
    args+=(--handover-file "$flag" --max-overtime "$overtime")
  fi
  began=$(date +%s)
  while :; do
    # The scanner needs no GitHub token; only this script talks to GitHub.
    rc=0
    env -u GH_TOKEN -u GITHUB_TOKEN python -m lootwatch "${args[@]}" || rc=$?
    [ "$rc" -gt "$code" ] && code=$rc
    [ -z "$finder" ] && break   # nobody is coming after this run: the shift is simply over
    [ -e "$flag" ] && break     # relieved, or told that no relief can be had
    elapsed=$(( $(date +%s) - began ))
    [ "$elapsed" -ge $((shift_seconds + overtime)) ] && break
    # The scanner ended although its relief is not there yet: it crashed. The runs must not stop
    # for that. Start it again a few times; if it will not stay up, at least wait for the relief,
    # so that the next run (on a fresh machine) gets its chance.
    restarts=$((restarts + 1))
    if [ "$restarts" -gt 5 ]; then
      say "the scanner keeps stopping (exit $rc); waiting for the next run without it"
      while [ ! -e "$flag" ] && [ $(( $(date +%s) - began )) -lt $((shift_seconds + overtime)) ]; do sleep "$POLL"; done
      break
    fi
    say "the scanner stopped early (exit $rc); starting it again in 20 seconds (restart $restarts)"
    sleep 20
    remaining=$(( shift_seconds - ( $(date +%s) - began ) ))
    [ "$remaining" -lt 30 ] && remaining=30
    args[1]=$remaining
  done
  if [ -n "$finder" ]; then
    kill "$finder" 2>/dev/null
    wait "$finder" 2>/dev/null
  fi
  return "$code"
}

case "${1:-}" in
  check) check_needed ;;
  wait) wait_turn ;;
  shift) run_shift ;;
  *) echo "usage: relay.sh check|wait|shift" >&2; exit 2 ;;
esac

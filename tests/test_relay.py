"""deploy/relay.sh: how runs on GitHub Actions take turns, played against a stand-in for GitHub.

The script only ever talks to GitHub through the `gh` command and starts the scanner as `python
-m lootwatch`. Here both are small stand-ins on the PATH: `gh` answers from files in a folder
(which runs exist, which of them stand ready) and records what it was asked to do; `python` is a
scanner that honours --max-runtime, --handover-file and --max-overtime and nothing else.

Every scenario takes a few seconds of real time, so they all run side by side, once, and the
tests then look at what happened in each.
"""

import concurrent.futures
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent / "deploy" / "relay.sh"

GH = r"""#!/usr/bin/env bash
S="${STATE:?}"
echo "gh $*" >> "$S/calls"
case "$1 $2" in
  "api repos/"*"/runs?"*) [ -f "$S/api_down" ] && exit 1; cat "$S/runs" 2>/dev/null; exit 0 ;;
  "api repos/"*"/jobs") id=$(echo "$2" | sed -E 's#.*/runs/([0-9]+)/jobs#\1#'); cat "$S/post_$id" 2>/dev/null || echo 0; exit 0 ;;
  "workflow run")
    [ -f "$S/refuse_dispatch" ] && exit 1
    n=$(cat "$S/next_id" 2>/dev/null || echo 2000); echo $((n + 1)) > "$S/next_id"
    [ -f "$S/never_listed" ] || echo "$n queued 0" >> "$S/runs"
    echo "$n $*" >> "$S/dispatched"; exit 0 ;;
  "run cancel") echo "$3" >> "$S/cancelled"; sed -i "/^$3 /d" "$S/runs"; exit 0 ;;
esac
exit 0
"""

SCANNER = r"""#!/usr/bin/env bash
run=0; flag=""; over=0
while [ $# -gt 0 ]; do case "$1" in --max-runtime) run=$2; shift 2;; --handover-file) flag=$2; shift 2;; --max-overtime) over=$2; shift 2;; *) shift;; esac; done
[ -n "${GH_TOKEN:-}" ] && echo "scanner: saw the GitHub token"
echo "scanner: started for ${run}s" >> "$STATE/scanner"
if [ -f "$STATE/crash_first" ]; then
  n=$(cat "$STATE/crashes" 2>/dev/null || echo 0)
  if [ "$n" -lt "$(cat "$STATE/crash_first")" ]; then echo $((n + 1)) > "$STATE/crashes"; sleep 1; echo "scanner: crashed"; exit 1; fi
fi
start=$(date +%s)
while [ $(( $(date +%s) - start )) -lt "$run" ]; do sleep 1; done
if [ -n "$flag" ]; then
  while [ ! -e "$flag" ] && [ $(( $(date +%s) - start )) -lt $((run + over)) ]; do sleep 1; done
  [ -e "$flag" ] && echo "scanner: handed over to $(cat "$flag")" || echo "scanner: overtime used up"
else
  echo "scanner: plain end"
fi
exit "${SCANNER_EXIT:-0}"
"""


class Scene:
    """One run of relay.sh in a folder of its own."""

    def __init__(self, root: Path, name: str, command: str, env=None, runs="", posted=(), files=(), script=None):
        self.name, self.command = name, command
        self.state = root / name
        self.state.mkdir()
        (self.state / "runs").write_text(runs, encoding="utf-8")
        (self.state / "output").write_text("", encoding="utf-8")
        for run in posted:
            (self.state / f"post_{run}").write_text("1", encoding="utf-8")
        for file_name, content in files:
            (self.state / file_name).write_text(content, encoding="utf-8")
        self.env = dict(os.environ, STATE=str(self.state), PATH=f"{root / 'bin'}:{os.environ['PATH']}",
                        GITHUB_REPOSITORY="o/r", GITHUB_RUN_ID="1000", GITHUB_REF_NAME="main",
                        GITHUB_OUTPUT=str(self.state / "output"), RUNNER_TEMP=str(self.state), GH_TOKEN="secret",
                        RELAY_POLL="1")
        self.env.pop("GITHUB_EVENT_NAME", None)
        self.env.update(env or {})
        self.script = script  # what "GitHub" does meanwhile: a function of this scene, run in a thread
        self.code, self.log, self.took = None, "", 0.0

    def play(self) -> "Scene":
        helper = threading.Thread(target=self.script, args=(self,), daemon=True) if self.script else None
        if helper:
            helper.start()
        started = time.time()
        done = subprocess.run(["bash", str(RELAY), self.command], env=self.env, capture_output=True, text=True, timeout=120)
        self.code, self.log, self.took = done.returncode, done.stdout + done.stderr, time.time() - started
        return self

    # -- what happened ------------------------------------------------------
    def read(self, name: str) -> str:
        path = self.state / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @property
    def surplus(self) -> bool:
        return "surplus=1" in self.read("output")

    @property
    def dispatched(self):
        return [line.split()[0] for line in self.read("dispatched").splitlines()]

    # -- what GitHub does meanwhile ------------------------------------------
    def set_runs(self, text: str) -> None:
        (self.state / "runs").write_text(text, encoding="utf-8")

    def when_listed(self, run: str, wait: float = 30.0) -> bool:
        deadline = time.time() + wait
        while time.time() < deadline:
            if any(line.split()[0] == run for line in self.read("runs").splitlines()):
                return True
            time.sleep(0.2)
        return False

    def bring_up(self, run: str) -> None:
        """The run gets a machine and reaches its waiting step."""
        self.set_runs("".join(line.replace(" queued ", " in_progress ") + "\n" if line.split()[0] == run else line + "\n"
                              for line in self.read("runs").splitlines()))
        (self.state / f"post_{run}").write_text("1", encoding="utf-8")

    def finish(self, run: str) -> None:
        self.set_runs("".join(line.replace(" in_progress ", " completed ") + "\n" if line.split()[0] == run else line + "\n"
                              for line in self.read("runs").splitlines()))


def _older_finishes(scene: Scene) -> None:
    time.sleep(3)
    scene.set_runs("900 completed 12\n")


def _relief_comes_up(scene: Scene) -> None:
    if scene.when_listed("2000"):
        time.sleep(1)
        scene.bring_up("2000")


def _second_relief_comes_up(scene: Scene) -> None:
    if scene.when_listed("2001"):
        time.sleep(1)
        scene.bring_up("2001")


def _relief_dies_then_another(scene: Scene) -> None:
    if scene.when_listed("2000"):
        time.sleep(1)
        scene.bring_up("2000")
        time.sleep(4)
        scene.finish("2000")
        _second_relief_comes_up(scene)


def _refused_for_a_while(scene: Scene) -> None:
    time.sleep(5)
    (scene.state / "refuse_dispatch").unlink()
    _relief_comes_up(scene)


def _listed_only_the_second_time(scene: Scene) -> None:
    deadline = time.time() + 30
    while time.time() < deadline and not scene.read("dispatched"):
        time.sleep(0.2)
    (scene.state / "never_listed").unlink()
    _second_relief_comes_up(scene)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Relay(unittest.TestCase):
    scenes = {}

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        (cls.root / "bin").mkdir()
        for name, body in (("gh", GH), ("python", SCANNER)):
            path = cls.root / "bin" / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        keep = {"KEEP_RUNNING": "1"}
        on_schedule = {"KEEP_RUNNING": "1", "GITHUB_EVENT_NAME": "schedule"}
        quick = {"KEEP_RUNNING": "1", "LOOP_SECONDS": "4", "RELAY_LEAD": "2", "RELAY_MAX_OVERTIME": "60"}
        plans = [
            # "Am I needed?"
            ("alone", "check", {}, "", (), ()),
            ("third", "check", {}, "900 in_progress 12\n950 in_progress 1\n", ("900", "950"), ()),
            ("deciding", "check", {}, "900 in_progress 12\n950 in_progress 0\n", ("900",), ()),
            ("schedule_alive", "check", on_schedule, "900 in_progress 12\n", ("900",), ()),
            ("schedule_stale", "check", on_schedule, "900 in_progress 70\n", ("900",), ()),
            ("schedule_dead", "check", on_schedule, "900 completed 30\n", (), ()),
            ("schedule_blind", "check", on_schedule, "", (), (("api_down", ""),)),
            ("dispatch_blind", "check", keep, "", (), (("api_down", ""),)),
            ("schedule_only", "check", {"GITHUB_EVENT_NAME": "schedule"}, "900 in_progress 3\n", ("900",), ()),
            # "Wait for my turn"
            ("wait_older", "wait", keep, "900 in_progress 12\n", ("900",), (), _older_finishes),
            ("wait_queued", "wait", keep, "900 queued 14\n800 completed 30\n1100 queued 0\n", (), ()),
            ("wait_limit", "wait", dict(keep, RELAY_WAIT_LIMIT="3"), "900 in_progress 70\n", ("900",), ()),
            ("wait_blind", "wait", dict(keep, RELAY_BLIND_AFTER="3"), "", (), (("api_down", ""),)),
            # the shift
            ("no_chain", "shift", {"LOOP_SECONDS": "2"}, "", (), ()),
            ("handover", "shift", quick, "", (), (), _relief_comes_up),
            ("stuck_in_queue", "shift", dict(quick, RELAY_PATIENCE="4"), "", (), (), _second_relief_comes_up),
            ("refused", "shift", quick, "", (), (("refuse_dispatch", ""),), _refused_for_a_while),
            ("relief_dies", "shift", dict(quick, LOOP_SECONDS="14", RELAY_LEAD="12", RELAY_LISTING_LAG="2"), "", (), (),
             _relief_dies_then_another),
            ("never_listed", "shift", dict(quick, RELAY_LISTING_LAG="2", RELAY_PATIENCE="40"), "", (),
             (("never_listed", ""),), _listed_only_the_second_time),
            ("already_waiting", "shift", quick, "1500 in_progress 3\n", ("1500",), ()),
            ("still_deciding", "shift", dict(quick, RELAY_MAX_OVERTIME="6", RELAY_PATIENCE="60"), "1500 in_progress 0\n", (), ()),
            ("nobody_comes", "shift", dict(quick, RELAY_MAX_OVERTIME="6", RELAY_PATIENCE="2", RELAY_MAX_STARTS="2"), "", (), ()),
            ("last_of_test_chain", "shift", dict(quick, RELAY_HOPS="0"), "", (), ()),
            ("passes_on", "shift", dict(quick, RELAY_HOPS="2", SHIFT_SECONDS_INPUT="120", DRY_RUN="1", SCANNER_EXIT="3",
                                         WATCHLIST_FILE="w.txt"), "", (), (), _relief_comes_up),
            ("crash_once", "shift", dict(quick, LOOP_SECONDS="26", RELAY_LEAD="4"), "", (), (("crash_first", "1"),),
             _relief_comes_up),
            ("crash_no_chain", "shift", {"LOOP_SECONDS": "3"}, "", (), (("crash_first", "9"),)),
        ]
        built = [Scene(cls.root, plan[0], plan[1], plan[2], plan[3], plan[4], plan[5], plan[6] if len(plan) > 6 else None)
                 for plan in plans]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(built)) as pool:
            cls.scenes = {scene.name: scene for scene in pool.map(Scene.play, built)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def scene(self, name: str) -> Scene:
        scene = self.scenes[name]
        self.assertIsNotNone(scene.code, name)
        return scene

    # -- is this run needed? ---------------------------------------------------
    def test_a_run_alone_is_needed(self):
        self.assertFalse(self.scene("alone").surplus)

    def test_a_third_run_leaves_only_when_one_scans_and_one_really_waits(self):
        self.assertTrue(self.scene("third").surplus)
        self.assertFalse(self.scene("deciding").surplus, "a run that is still deciding does not count as waiting")

    def test_a_scheduled_run_only_restarts_what_has_stopped(self):
        self.assertTrue(self.scene("schedule_alive").surplus)
        self.assertIn("nothing to restart", self.scene("schedule_alive").log)
        self.assertFalse(self.scene("schedule_dead").surplus)
        self.assertFalse(self.scene("schedule_stale").surplus, "an hour-old run on duty is taken for stuck")
        self.assertFalse(self.scene("schedule_only").surplus, "without KEEP_RUNNING the schedule is the monitor")

    def test_not_knowing_who_runs_a_scheduled_run_stays_away_and_a_started_one_waits(self):
        self.assertTrue(self.scene("schedule_blind").surplus)
        self.assertFalse(self.scene("dispatch_blind").surplus)

    # -- waiting ------------------------------------------------------------------
    def test_waits_until_the_older_run_has_finished(self):
        scene = self.scene("wait_older")
        self.assertEqual(scene.code, 0)
        self.assertGreater(scene.took, 2.5)
        self.assertIn("my turn after", scene.log)

    def test_older_runs_that_never_got_a_machine_are_cancelled(self):
        scene = self.scene("wait_queued")
        self.assertEqual(scene.read("cancelled").split(), ["900"], "not the finished one, not the younger one")

    def test_takes_over_from_a_run_that_does_not_finish_and_starts_blind_only_after_a_long_silence(self):
        self.assertIn("taking over", self.scene("wait_limit").log)
        blind = self.scene("wait_blind")
        self.assertIn("starting without knowing who else runs", blind.log)
        self.assertGreater(blind.took, 2.0)

    # -- the shift ----------------------------------------------------------------
    def test_without_keep_running_no_next_run_is_started(self):
        scene = self.scene("no_chain")
        self.assertEqual((scene.code, scene.dispatched), (0, []))
        self.assertIn("scanner: plain end", scene.log)

    def test_the_run_on_duty_leaves_only_when_its_relief_stands_ready(self):
        scene = self.scene("handover")
        self.assertEqual(scene.dispatched, ["2000"])
        self.assertIn("scanner: handed over to 2000", scene.log)
        self.assertNotIn("saw the GitHub token", scene.log, "the scanner is not given the token")

    def test_a_relief_stuck_in_the_queue_is_replaced(self):
        scene = self.scene("stuck_in_queue")
        self.assertEqual(scene.dispatched, ["2000", "2001"])
        self.assertIn("scanner: handed over to 2001", scene.log)

    def test_a_refusal_from_github_means_staying_on_duty(self):
        scene = self.scene("refused")
        self.assertIn("staying on duty", scene.log)
        self.assertIn("scanner: handed over to 2000", scene.log)
        self.assertNotIn("none", scene.read("relief-ready-1000"))

    def test_a_relief_that_disappears_before_the_handover_is_replaced(self):
        scene = self.scene("relief_dies")
        self.assertIn("run 2000 is gone before it could take over", scene.log)
        self.assertIn("scanner: handed over to 2001", scene.log)

    def test_a_started_run_that_never_shows_up_is_started_again(self):
        scene = self.scene("never_listed")
        self.assertEqual(scene.dispatched, ["2000", "2001"])
        self.assertIn("scanner: handed over to 2001", scene.log)

    def test_a_run_already_waiting_is_the_relief_but_one_still_deciding_is_not(self):
        waiting = self.scene("already_waiting")
        self.assertEqual(waiting.dispatched, [])
        self.assertIn("scanner: handed over to 1500", waiting.log)
        deciding = self.scene("still_deciding")
        self.assertNotIn("handed over to 1500", deciding.log)
        self.assertIn("scanner: overtime used up", deciding.log)

    def test_when_nobody_comes_scanning_goes_on_for_the_whole_overtime(self):
        scene = self.scene("nobody_comes")
        self.assertEqual(len(scene.dispatched), 2, "no more starts than allowed")
        self.assertIn("scanner: overtime used up", scene.log)
        self.assertGreater(scene.took, 9.0)

    def test_test_chains_end_and_pass_their_settings_on(self):
        self.assertEqual(self.scene("last_of_test_chain").dispatched, [])
        scene = self.scene("passes_on")
        self.assertEqual(scene.code, 3, "the scanner's exit code is the step's")
        call = scene.read("dispatched")
        self.assertIn("--ref main -f mode=run -f hops=1 -f shift_seconds=120 -f watchlist_file=w.txt", call)
        self.assertNotIn("dry_run", call, "a dry run is decided by the branch, never handed on")

    def test_a_crashed_scanner_is_started_again_and_the_handover_still_happens(self):
        scene = self.scene("crash_once")
        self.assertIn("the scanner stopped early (exit 1); starting it again", scene.log)
        self.assertIn("scanner: handed over to 2000", scene.log)
        self.assertEqual(scene.code, 1, "the run shows as failed")
        self.assertEqual(len(scene.read("scanner").splitlines()), 2)
        plain = self.scene("crash_no_chain")
        self.assertEqual((plain.code, len(plain.read("scanner").splitlines())), (1, 1), "no loop when nothing comes after")


if __name__ == "__main__":
    unittest.main()

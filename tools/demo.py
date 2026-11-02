"""Guided live demo / acceptance test of the whole MedVerify system.

    .\\mv demo              all scenarios
    .\\mv demo S3 S6        only some

Each scenario sets something up (e.g. opens a test dose, so the light and buzzer fire), tells
you exactly what to do with your hands, then watches what the system concludes and how fast.
It asks you only what software cannot know ("did LED A light up?"). Results are printed as a
table and saved to data/demo/<time>.json.

Needs: `mv up` running, the real box (device_mode serial), and the camera calibrated WITH pill
counts (`mv cam loaded A=4 B=4 C=4 D=4`, using the real number of pieces in each compartment).
Use pieces of equal size within a compartment, in a single layer.

Test doses are labelled "Demo"; they produce real alerts in the caretaker app.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
WATCH_KINDS = {"UNSCHEDULED_REMOVAL", "REFILLED", "PUT_BACK", "TOO_MANY", "WRONG_COMPARTMENT", "DOSE_REMOVED"}


@dataclass
class Result:
    id: str
    title: str
    passed: bool | None          # None = skipped
    detail: str = ""
    seconds: float | None = None  # time from "done" to the system's conclusion
    observations: dict[str, Any] = field(default_factory=dict)


class Demo:
    def __init__(self, http, ask: Callable[[str], str] = input, out: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 poll: float = 0.5) -> None:
        self.http, self.ask, self.out, self.clock, self.sleep, self.poll = http, ask, out, clock, sleep, poll

    # ------------------------------------------------------------- helpers
    def status(self) -> dict:
        return self.http.get("/api/status")

    def counts(self) -> dict[str, int | None]:
        return {c: v.get("count") for c, v in self.status()["doses"]["compartments"].items()}

    def loaded(self) -> dict[str, int | None]:
        return {c: v.get("loaded_count") for c, v in self.status()["doses"]["compartments"].items()}

    def note_ids(self) -> set[str]:
        return {n["id"] for n in self.status()["notifications"]}

    def new_notes(self, before: set[str]) -> list[dict]:
        return [n for n in self.status()["notifications"] if n["id"] not in before]

    def session(self, session_id: str) -> dict | None:
        doses = self.status()["doses"]
        for s in doses["active_sessions"] + doses["recent_sessions"]:
            if s["id"] == session_id:
                return s
        return None

    def start_dose(self, compartment: str, pills: int = 1) -> str:
        return self.http.post("/api/reminders", {"compartment_id": compartment, "label": "Demo",
                                                 "pills": pills})["session"]["id"]

    def cancel_all(self) -> None:
        self.http.post("/api/device/stop", {})

    def wait(self, check: Callable[[], Any], timeout: float) -> tuple[Any, float | None]:
        start = self.clock()
        while self.clock() - start < timeout:
            value = check()
            if value:
                return value, round(self.clock() - start, 1)
            self.sleep(self.poll)
        return None, None

    def act(self, instruction: str) -> None:
        self.out(f"\n  >>> {instruction}")
        self.ask("      Press Enter when done (hand out of view)... ")

    def yes(self, question: str) -> bool:
        while True:
            answer = self.ask(f"      {question} [y/n] ").strip().lower()
            if answer in ("y", "n"):
                return answer == "y"

    def restore(self, compartment: str, what: str) -> bool:
        """Ask for pieces to go back, and wait for the camera to count the compartment full again."""
        self.act(f"Put {what} back into compartment {compartment}.")
        target = self.loaded().get(compartment)
        ok, _ = self.wait(lambda: self.counts().get(compartment) == target, 30)
        if not ok:
            self.out(f"      ! Camera counts {self.counts().get(compartment)} in {compartment}, expected {target}. "
                     "Fix the pieces, or recalibrate later with `mv cam loaded`.")
        return bool(ok)

    # ------------------------------------------------------------ scenarios
    def s1_hardware(self) -> Result:
        title = "Box: all LEDs, on-board LED and buzzer"
        if self.status()["device"].get("mode") != "serial":
            return Result("S1", title, None, "skipped: server uses a simulated box")
        self.out("\n  Watch the board for 12 seconds...")
        self.http.post("/api/device/diag", {}, timeout=40)
        ok = self.yes("Did all four LEDs, the ESP32's blue LED and the 2 s tone work?")
        return Result("S1", title, ok, "confirmed by you" if ok else "you saw a fault: run `mv box diag`")

    def s2_idle(self) -> Result:
        before_counts, before_notes = self.counts(), self.note_ids()
        self.out("\n  >>> Keep your hands away from the sheet for 20 seconds.")
        self.sleep(20)
        changed = {c: (before_counts[c], n) for c, n in self.counts().items() if n != before_counts.get(c)}
        notes = [n["kind"] for n in self.new_notes(before_notes) if n["kind"] in WATCH_KINDS]
        ok = not changed and not notes
        return Result("S2", "Untouched box: no false alarms in 20 s", ok,
                      "no change" if ok else f"counts changed {changed}, alerts {notes}")

    def _dose(self, rid: str, title: str, compartment: str, pills: int, action: str,
              check: Callable[[dict], bool], what_back: str, ask_led: bool = False) -> Result:
        sid = self.start_dose(compartment, pills)
        led = self.yes(f"Is LED {compartment} on (and did it beep)?") if ask_led else None
        self.act(action)
        session, secs = self.wait(lambda: (s := self.session(sid)) and s["status"] == "CLOSED" and s, 30)
        session = session or self.session(sid)
        ok = bool(session) and check(session)
        led_off = self.yes(f"Did LED {compartment} turn off?") if ask_led and session["status"] == "CLOSED" else None
        if session and session["status"] == "ACTIVE":
            self.cancel_all()
        detail = f"outcome {session['outcome'] or 'still open'}, flags {session['flags'] or 'none'}"
        obs = {"led_on": led, "led_off": led_off}
        if ask_led:
            ok = ok and bool(led) and bool(led_off)
        self.restore(compartment, what_back)
        return Result(rid, title, ok, detail, secs, obs)

    def s3_dose_taken(self) -> Result:
        return self._dose("S3", "Dose: take exactly 1 pill from A", "A", 1,
                          "Take ONE piece out of compartment A and keep it out of view.",
                          lambda s: s["outcome"] == "REMOVED" and not any(f.startswith("TOO_MANY") for f in s["flags"]),
                          "the piece", ask_led=True)

    def s4_put_back(self) -> Result:
        sid = self.start_dose("B", 1)
        before = self.note_ids()
        self.act("Take ONE piece out of B, hold it away for 5 seconds, then put it back ANYWHERE in B.")
        self.sleep(6)
        session = self.session(sid)
        flagged = "PUT_BACK" in session["flags"]
        back = self.counts().get("B") == self.loaded().get("B")
        # Correct if the system did not end up believing the dose was taken without noticing the return
        ok = back and (session["status"] == "ACTIVE" or flagged)
        if session["status"] == "ACTIVE":
            self.cancel_all()
        notes = [n["kind"] for n in self.new_notes(before)]
        return Result("S4", "Taken and put back: not counted as a dose", ok,
                      f"dose {session['status'].lower()}{' + PUT_BACK' if flagged else ''}, count back: {back}, "
                      f"alerts {notes}")

    def s5_wrong_compartment(self) -> Result:
        sid = self.start_dose("C", 1)
        self.act("Take ONE piece out of D (the WRONG compartment).")
        flagged, secs = self.wait(lambda: "WRONG_COMPARTMENT:D" in (self.session(sid) or {}).get("flags", []), 30)
        self.restore("D", "that piece")
        self.act("Now take ONE piece out of C (the right one).")
        session, _ = self.wait(lambda: (s := self.session(sid)) and s["outcome"] == "REMOVED" and s, 30)
        if not session:
            self.cancel_all()
        self.restore("C", "that piece")
        ok = bool(flagged) and bool(session)
        return Result("S5", "Wrong compartment warned, then right one accepted", ok,
                      f"warning {'yes' if flagged else 'NO'}, dose {'taken' if session else 'NOT closed'}", secs)

    def s6_too_many(self) -> Result:
        return self._dose("S6", "Too many: take 2 when the dose is 1", "A", 1,
                          "Take TWO pieces out of compartment A.",
                          lambda s: any(f.startswith("TOO_MANY") for f in s["flags"]), "both pieces")

    def _no_change(self, rid: str, title: str, instruction: str, wait_s: float) -> Result:
        before_counts, before_notes = self.counts(), self.note_ids()
        self.act(instruction)
        self.sleep(wait_s)
        changed = {c: (before_counts[c], n) for c, n in self.counts().items() if n != before_counts.get(c)}
        notes = [n["kind"] for n in self.new_notes(before_notes) if n["kind"] in WATCH_KINDS]
        ok = not changed and not notes
        return Result(rid, title, ok, "no change" if ok else f"counts changed {changed}, alerts {notes}")

    def s7_rearrange(self) -> Result:
        return self._no_change("S7", "Rearranging pills is not a removal",
                               "Slide the pieces in B around INSIDE B (take none out).", 10)

    def s8_hand_hover(self) -> Result:
        return self._no_change("S8", "A hand resting over a compartment is not a removal",
                               "Hold your hand still over C for 5 seconds, take nothing, then move it away.", 10)

    SCENARIOS = {"S1": "s1_hardware", "S2": "s2_idle", "S3": "s3_dose_taken", "S4": "s4_put_back",
                 "S5": "s5_wrong_compartment", "S6": "s6_too_many", "S7": "s7_rearrange", "S8": "s8_hand_hover"}

    # ------------------------------------------------------------------ run
    def preflight(self) -> list[str]:
        s = self.status()
        problems = []
        cam = s["doses"]["camera"]
        if not cam.get("online") or not cam.get("monitoring"):
            problems.append("The camera is not monitoring: `mv up`, then calibrate (`mv cam empty`, `mv cam loaded ...`).")
        missing = [c for c, v in s["doses"]["compartments"].items() if v.get("count") is None]
        if missing:
            problems.append(f"No pill counts for {', '.join(missing)}: recalibrate with counts, e.g. "
                            "`mv cam loaded A=4 B=4 C=4 D=4` (the real number of pieces).")
        if s["doses"]["active_sessions"]:
            problems.append("A dose is open: cancel it first (`mv dose cancel`).")
        return problems

    def run(self, only: list[str] | None = None) -> list[Result]:
        problems = self.preflight()
        if problems:
            for p in problems:
                self.out(f"Cannot start: {p}")
            return []
        self.out("This opens test doses labelled 'Demo' (lights, buzzer, caretaker alerts).")
        if not self.yes("Start?"):
            return []
        results = []
        for rid, name in self.SCENARIOS.items():
            if only and rid not in only:
                continue
            self.out(f"\n=== {rid}: {getattr(self, name).__doc__ or name}")
            try:
                results.append(getattr(self, name)())
            except Exception as exc:  # A failing step must not lose the results so far
                results.append(Result(rid, name, False, f"error: {type(exc).__name__}: {exc}"))
                self.cancel_all()
            r = results[-1]
            mark = "PASS" if r.passed else ("skip" if r.passed is None else "FAIL")
            self.out(f"  [{mark}] {r.title}: {r.detail}" + (f" ({r.seconds}s)" if r.seconds is not None else ""))
        return results


def report(results: list[Result], out: Callable[[str], None] = print) -> Path | None:
    if not results:
        return None
    out("\n" + "=" * 72)
    passed = sum(1 for r in results if r.passed)
    run = sum(1 for r in results if r.passed is not None)
    for r in results:
        mark = "PASS" if r.passed else ("skip" if r.passed is None else "FAIL")
        out(f"{mark:4}  {r.id}  {r.title:<55} {'' if r.seconds is None else f'{r.seconds}s'}")
    out(f"{passed}/{run} passed")
    folder = ROOT / "data" / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    out(f"Saved {path}")
    return path


class RequestsHTTP:
    def __init__(self, base: str) -> None:
        import requests
        self.base, self.requests = base, requests

    def get(self, path: str) -> dict:
        return self.requests.get(self.base + path, timeout=15).json()

    def post(self, path: str, body: dict, timeout: float = 15) -> dict:
        r = self.requests.post(self.base + path, json=body, timeout=timeout)
        data = r.json()
        if not r.ok or data.get("ok") is False:
            raise RuntimeError(data.get("error") or f"HTTP {r.status_code}")
        return data

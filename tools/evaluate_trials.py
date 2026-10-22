"""Grade recorded trials and find the thresholds that make the fewest costly mistakes.

    .venv\\Scripts\\python.exe tools\\evaluate_trials.py              report on data/trials
    .venv\\Scripts\\python.exe tools\\evaluate_trials.py --rescore    recompute scores from saved frames first
    .venv\\Scripts\\python.exe tools\\evaluate_trials.py --apply      write the best thresholds to settings.json

Writes data/trials/report.md as well as printing it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision.trial_eval import (  # noqa: E402
    CATEGORIES, Grade, Params, grade, load_trials, rescore, separability, sweep,
)

SETTINGS = ROOT / "config" / "settings.json"


def grade_lines(g: Grade, title: str) -> list[str]:
    p = g.params
    lines = [
        f"### {title}",
        f"change_threshold={p.change_threshold}, match_threshold={p.match_threshold}, "
        f"debounce_frames={p.debounce_frames}, confirm_seconds={p.confirm_seconds}",
        "",
        f"- Correct: **{g.correct}/{g.total}** ({g.accuracy:.0%})",
        f"- Missed removals: **{g.removal_missed}**  (dose emptied, camera did not say so)",
        f"- False removals: **{g.false_removal}**  (camera said emptied, it was not)",
        f"- Other mistakes: {g.other_errors}  (changed vs loaded confusion, or unclear)",
        "",
        "| you did \\ camera said | " + " | ".join(CATEGORIES) + " |",
        "|---|" + "---|" * len(CATEGORIES),
    ]
    for label, row in g.confusion.items():
        lines.append(f"| {label} | " + " | ".join(str(row[c]) for c in CATEGORIES) + " |")
    return lines + [""]


def build_report(trials, skipped, base: Params, rescored: bool) -> tuple[list[str], Grade]:
    lines = ["# MedVerify trial report", ""]
    lines.append(f"{len(trials)} trial(s) graded{' (scores recomputed from frames)' if rescored else ''}; "
                 f"{len(skipped)} skipped.")
    for s in skipped:
        lines.append(f"- skipped {s}")
    lines.append("")
    if not trials:
        lines.append("No finished trials yet. In the camera window: T, label with 1-4, N, act, F.")
        return lines, grade([], base)

    current = grade(trials, base)
    lines += grade_lines(current, "Current settings")

    lines.append("### How well the scores separate (last evaluation of each trial)")
    lines.append("If the 'removed' and 'unchanged' ranges overlap, no threshold can be perfect: "
                 "the comparator itself needs work.")
    lines.append("")
    lines.append("| you did | diff vs loaded (min / median / max) | diff vs empty (min / median / max) | n |")
    lines.append("|---|---|---|---|")
    for label, by_key in separability(trials).items():
        e = by_key.get("diff_from_expected", {})
        m = by_key.get("diff_from_empty", {})
        fmt = lambda d: f"{d['min']} / {d['median']} / {d['max']}" if d else "n/a"  # noqa: E731
        lines.append(f"| {label} | {fmt(e)} | {fmt(m)} | {e.get('n', m.get('n', 0))} |")
    lines.append("")

    ranked = sweep(trials, base)
    best = ranked[0]
    lines.append("### Best threshold settings (lowest cost: missed/false removals count 3x)")
    lines.append("")
    lines.append("| change | match | correct | missed removals | false removals | other | cost |")
    lines.append("|---|---|---|---|---|---|---|")
    for g in ranked[:8]:
        p = g.params
        lines.append(f"| {p.change_threshold} | {p.match_threshold} | {g.correct}/{g.total} | "
                     f"{g.removal_missed} | {g.false_removal} | {g.other_errors} | {g.cost} |")
    lines.append("")
    if best.cost < current.cost:
        lines.append(f"Suggestion: change_threshold={best.params.change_threshold}, "
                     f"match_threshold={best.params.match_threshold} "
                     f"(cost {current.cost} -> {best.cost}). Run with --apply to save it.")
    else:
        lines.append("The current settings are already among the best for these trials.")
    lines.append("")

    if current.failures:
        lines.append("### Mistakes with current settings")
        for f in current.failures:
            lines.append(f"- {f['trial']} compartment {f['slot']}: you did '{f['label']}', camera ended at {f['got']}")
        lines.append("")
    if len(trials) < 20:
        lines.append(f"Note: only {len(trials)} trials. Thresholds tuned on fewer than ~20 trials "
                     "(with every label represented) will not generalise; keep collecting.")
    return lines, best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", default=str(ROOT / "data" / "trials"))
    ap.add_argument("--rescore", action="store_true", help="recompute scores from saved frames")
    ap.add_argument("--include-flagged", action="store_true", help="include trials that started out of calibration")
    ap.add_argument("--apply", action="store_true", help="save the best thresholds to config/settings.json")
    args = ap.parse_args()

    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    base = Params(
        change_threshold=float(settings.get("change_threshold", 0.08)),
        match_threshold=float(settings.get("match_threshold", 0.05)),
        debounce_frames=int(settings.get("debounce_frames", 2)),
        confirm_seconds=float(settings.get("confirm_seconds", 2.0)),
    )
    trials, skipped = load_trials(args.trials, include_flagged=args.include_flagged)
    if args.rescore:
        trials = [rescore(t) for t in trials]

    lines, best = build_report(trials, skipped, base, args.rescore)
    text = "\n".join(lines)
    print(text)
    out = Path(args.trials) / "report.md"
    if Path(args.trials).exists():
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\nSaved {out}")

    if args.apply and trials:
        settings["change_threshold"] = best.params.change_threshold
        settings["match_threshold"] = best.params.match_threshold
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print(f"Applied change_threshold={best.params.change_threshold}, "
              f"match_threshold={best.params.match_threshold} to {SETTINGS}")


if __name__ == "__main__":
    main()

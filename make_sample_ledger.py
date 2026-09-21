"""Generate the synthetic ledger in sample-ledger/ so guard_stats.py runs on a fresh clone.

    python make_sample_ledger.py

These rows are FABRICATED. They exist so a reader can execute the measurement rather than
take a number on trust, and so the counting definition in guard_stats.py can be checked
against input you can read. They are not a record of anything that happened.

The shape is deliberately awkward in the two ways real ledgers are awkward:

  * Some rows carry no `decision` field at all. Those are not judged calls and must be
    excluded from the evaluation count, not silently folded in. That distinction is the
    reason guard_stats.py exists rather than a one-line `wc -l`.

  * Refusals are RARE. A guard that refuses often is a guard fighting its caller; the
    interesting property of a real one is that it mostly allows, so its silence has to be
    distinguishable from it being dead.
"""

import json
import pathlib
import random

OUT = pathlib.Path(__file__).resolve().parent / "sample-ledger"

# gate name -> (judged rows, refusals, unjudged rows)
GATES = {
    "tool_selection": (8_400, 31, 260),
    "db_write_gate": (5_100, 9, 95),
    "node_route": (9_250, 44, 410),
}

REFUSAL_REASONS = [
    "wrong-surface", "unverified-claim", "absence-claim-unearned",
    "off-contract-tool", "destructive-without-preflight",
]
ALLOW_DECISIONS = ["allow", "allowed", "ok", "pass"]


def main():
    OUT.mkdir(exist_ok=True)
    rng = random.Random(20260920)          # fixed, so the sample is reproducible
    day_start = "2026-06-26"
    total_judged = total_refused = total_unjudged = 0

    for gate, (judged, refused, unjudged) in GATES.items():
        rows = []
        for i in range(judged):
            allowed = i >= refused
            rows.append({
                "ts": _stamp(rng, day_start),
                "gate": gate,
                "decision": rng.choice(ALLOW_DECISIONS) if allowed
                            else rng.choice(REFUSAL_REASONS),
                "tool": rng.choice(["Bash", "Write", "Edit", "Read", "PowerShell"]),
            })
        for _ in range(unjudged):
            # No `decision` key at all. An unjudged event is not an evaluation.
            rows.append({"ts": _stamp(rng, day_start), "gate": gate, "note": "observed, not judged"})

        rng.shuffle(rows)
        path = OUT / f"{gate}_events.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

        total_judged += judged
        total_refused += refused
        total_unjudged += unjudged
        print(f"  {path.name:28s} judged={judged:6d} refused={refused:3d} unjudged={unjudged}")

    print(f"\ntotals: judged={total_judged} refused={total_refused} unjudged={total_unjudged}")
    print("run `python guard_stats.py` to see the measurement read these back")


def _stamp(rng, start_day):
    year, month, day = (int(p) for p in start_day.split("-"))
    day_offset = rng.randint(0, 83)        # 84 distinct days
    d = day + day_offset
    m = month
    while d > 30:
        d -= 30
        m += 1
    return f"{year:04d}-{m:02d}-{d:02d}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}Z"


if __name__ == "__main__":
    main()

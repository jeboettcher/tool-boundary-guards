#!/usr/bin/env python3
"""guard_stats.py -- regenerate the guard figure quoted on the operator's resume, with its DEFINITION on the page.

Why this exists (2026-09-13): the resume says "the guards logged N evaluations and refused M calls."
The first version of that number was computed in a throwaway shell heredoc and never saved, so an
outward-facing claim had no artifact behind it. an independent reviewer asked for the producing script during review,
could not be served one, and reconstructed the aggregation from raw logs instead -- turning up a
definitional ambiguity nobody had written down (below). A number on a resume whose whole brand is
"I can say how often each guard fired" needs a command that regenerates it and agrees on what was
counted. "How did you count that?" is a guaranteed interview question, and a good one.

THE DEFINITION, decided and stated out loud:
  * EVALUATIONS = every recorded decision by a tool-boundary adherence gate (memory/.adherence/*.jsonl).
    These are the guards sitting between the model and an action, judging each call.
  * REFUSALS    = those same events whose decision is anything other than allow.
  * The credential/env-switch guard (runtime/env-switch.jsonl) is REPORTED SEPARATELY and is NOT folded
    into the headline pair. Its refusals are policy denials about which credential set is in scope, not
    instances of catching an agent about to do the wrong thing. Folding them in would inflate the
    refusal count with a different kind of event. That is a judgement call, so it is made explicitly
    here rather than left implicit in a number.
  * Events carrying NO decision field are counted separately and EXCLUDED -- an unjudged event is not
    an evaluation. This is the gap that made the original inline count and an independent reviewer's reconstruction
    disagree, and it is the reason this script exists rather than a one-liner.

Usage:
    python guard_stats.py                 # reads sample-ledger/ shipped beside this file
    python guard_stats.py --ledger DIR    # point it at your own *_events.jsonl
    python guard_stats.py --json
"""
import json
import pathlib
import sys
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
SAMPLE_LEDGER = HERE / "sample-ledger"


def _resolve_ledger(argv):
    """Decide which ledger directory to read, and SAY which one, always.

    The original version of this script walked upward looking for a private repo layout
    and hard-exited if it was not found. That was correct for its author and useless for
    anyone else: a reader who cloned this and ran it got a SystemExit where the document
    promises a number.

    Worse was the version before that, which resolved to the wrong directory, found no
    ledgers, and printed a confident, correctly-formatted "0 evaluations, 0 refusals".
    A clean success line over a real failure is the exact defect this repository is about.
    So: resolve explicitly, print the source, and refuse to report zero silently.
    """
    if "--ledger" in argv:
        i = argv.index("--ledger")
        if i + 1 >= len(argv):
            raise SystemExit("guard_stats: --ledger needs a directory")
        chosen = pathlib.Path(argv[i + 1]).expanduser().resolve()
        if not chosen.is_dir():
            raise SystemExit("guard_stats: not a directory: %s" % chosen)
        return chosen, "explicit --ledger"

    if SAMPLE_LEDGER.is_dir() and any(SAMPLE_LEDGER.glob("*_events.jsonl")):
        return SAMPLE_LEDGER, "the synthetic sample shipped with this repo"

    raise SystemExit(
        "guard_stats: no ledger found.\n"
        "  looked for: %s/*_events.jsonl\n"
        "  pass --ledger DIR to point at your own gate events." % SAMPLE_LEDGER)


ADHERENCE, LEDGER_SOURCE = _resolve_ledger(sys.argv)
ENV_SWITCH = ADHERENCE / "env-switch.jsonl"    # optional; absent in the sample
ALLOWED = {"allow", "allowed", "ok", "pass"}


def _rows(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue          # a torn final line is a log artifact, not a data point


def collect():
    per_gate, refused, no_decision = Counter(), Counter(), Counter()
    days = set()
    for f in sorted(ADHERENCE.glob("*_events.jsonl")):
        gate = f.stem.replace("_events", "")
        for r in _rows(f):
            dec = str(r.get("decision", "")).strip().lower()
            if not dec:
                no_decision[gate] += 1
                continue
            per_gate[gate] += 1
            if dec not in ALLOWED:
                refused[gate] += 1
            ts = r.get("ts")
            if isinstance(ts, str) and len(ts) >= 10:
                days.add(ts[:10])
    env_total = env_refused = 0
    for r in _rows(ENV_SWITCH):
        dec = str(r.get("decision", "")).strip().lower()
        if not dec:
            continue
        env_total += 1
        if dec not in ALLOWED:
            env_refused += 1
    return per_gate, refused, no_decision, days, env_total, env_refused


def main():
    per_gate, refused, no_decision, days, env_total, env_refused = collect()
    ev, rf = sum(per_gate.values()), sum(refused.values())
    span = sorted(days)
    out = {
        "evaluations": ev,
        "refusals": rf,
        "first_day": span[0] if span else None,
        "last_day": span[-1] if span else None,
        "distinct_days": len(span),
        "excluded_no_decision": sum(no_decision.values()),
        "env_switch_evaluations": env_total,
        "env_switch_refusals": env_refused,
        "per_gate": dict(per_gate),
        "per_gate_refusals": dict(refused),
    }
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
        return 0
    print("GUARD STATS -- tool-boundary adherence gates")
    print("  source      : %s" % ADHERENCE)
    print("                (%s)" % LEDGER_SOURCE)
    if ev == 0:
        # Never let a zero pass as a result. Zero here means the ledger was empty or the
        # rows carried no decision field, which is a fact about the INPUT, not about any
        # guard's behaviour, and the difference is the entire point of this script.
        print("  NO JUDGED EVENTS FOUND -- this says nothing about any guard.")
    print("  window      : %s -> %s (%d distinct days carrying events)"
          % (out["first_day"], out["last_day"], out["distinct_days"]))
    for g in sorted(per_gate):
        print("    %-22s evaluated=%7d  refused=%d" % (g, per_gate[g], refused[g]))
    print("  EVALUATIONS : %d" % ev)
    print("  REFUSALS    : %d" % rf)
    if out["excluded_no_decision"]:
        print("  (excluded %d event(s) with no decision field -- not judged calls)"
              % out["excluded_no_decision"])
    print("  credential/env guard, reported SEPARATELY, NOT in the figures above:")
    print("    evaluated=%d refused=%d" % (env_total, env_refused))
    if env_total and env_refused == env_total:
        print("    ^ 100%% refusal is implausible -- this ledger almost certainly uses a different")
        print("      `decision` vocabulary than the adherence gates. DO NOT quote this pair anywhere")
        print("      until its schema is checked. Flagged rather than silently printed.")
    print()
    if ADHERENCE == SAMPLE_LEDGER:
        # These figures come from fabricated rows. Printing a paste-ready sentence under
        # them would be handing someone a fake credential in a copyable format, which is
        # a strange thing for this repository of all repositories to do.
        print("SOURCE IS THE SYNTHETIC SAMPLE -- these numbers describe made-up rows.")
        print("They demonstrate the MEASUREMENT, not any guard's real behaviour.")
        print("Point --ledger at your own gate events to get a figure that means something.")
        return 0
    print("RESUME SENTENCE (paste this, do not retype the numbers):")
    print("  In their first %d days the guards logged %s evaluations and refused %d calls."
          % (out["distinct_days"], format(ev, ","), rf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

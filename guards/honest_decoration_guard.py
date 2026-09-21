#!/usr/bin/env python3
"""
honest_decoration_guard.py -- Stop hook: catch self-attestation of honesty used as DECORATION in
my just-finished reply and BLOCK the turn so I restate plainly before it stands.

Why this exists: the operator has corrected me FOUR times (2026-06-08, -16, -25, -27) for using
"honestly / to be honest / let me be straight / the honest truth" as a credibility garnish -- the
con-man/preacher tell. The rule lived on disk but kept failing to bind IN-SESSION. This hook makes
it bind at OUTPUT time. (feedback_drop_the_word_honest.md + feedback_honest_is_not_a_decoration.md.)

The rule it enforces: don't CLAIM honesty. Using "honest" to DISCUSS the value, to describe
someone, or to ASK the operator to be honest is FINE. Only self-attestation-as-garnish is banned.

False-positive defenses (the operator flagged the over-broad v1 on 2026-06-27):
  1. Quoted/backticked spans are stripped before scanning (use-vs-mention: quoting the rule is fine).
  2. The adverbials ("honestly / to be honest / truthfully / candidly / frankly") are only caught in
     the DISCOURSE-MARKER SHAPE -- set off by a sentence boundary and/or commas (e.g. "Honestly,",
     ", to be honest,"). So "she answered honestly", "I need you to be honest", "we want to be
     honest about X" do NOT trip it -- only the parenthetical garnish does.
  3. Only phrases with no legit non-decorative use ("let me be honest/real/straight", "in all
     honesty", "I'll be honest", "I'm being honest", "honestly speaking") are matched anywhere.

Blocks once per turn (respects stop_hook_active). Fail-open everywhere: any error -> exit 0 (allow).
"""
import sys, os, json, re, datetime

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.path.join(HOOK_DIR, "..", "..", ".."))
LOG = os.path.join(HOME, "runtime", "honest-guard.log")
sys.path.insert(0, HOOK_DIR)
import honesty_common  # shared plumbing: last_assistant_text / log / beat (2026-07-23 de-dup)

# Shape of a "set-off" discourse marker: at a sentence boundary, or comma-framed.
_LEAD = r"(?:^|[.!?;:]\s+|\n\s*|,\s*)"
_TAIL = r"\s*[,.?:;!]"  # a trailing punctuation mark = parenthetical/marker use
_ADV = r"(?:honestly|truthfully|candidly|frankly|to be (?:perfectly |totally |completely |brutally |fully )?honest)"

PATTERNS = [
    # adverbial/phrasal garnish ONLY in discourse-marker shape (comma/boundary framed)
    _LEAD + r"(" + _ADV + r")" + _TAIL,
    # self-attestation phrases with no legit non-decorative use -> match anywhere
    r"\b(i(?:'|’)?ll be honest|i will be honest)\b",
    r"\b(i(?:'|’)?m being honest|i am being honest)\b",
    r"\b(in all honesty)\b",
    r"\b(let me be (?:honest|real|straight|candid|frank))\b",
    r"\b(honest(?:ly)? speaking)\b",
    r"\b(being honest with you)\b",
]
RX = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in PATTERNS]

# Strip quoted/backticked spans (use-vs-mention) before scanning.
QUOTED = re.compile(r"`[^`]*`|\"[^\"]*\"|“[^”]*”|‘[^’]*’|'[^']{0,80}'", re.DOTALL)


def log(msg):
    honesty_common.log(LOG, msg)


def last_assistant_text(tp):
    return honesty_common.last_assistant_text(tp)


def find_decoration(text):
    scrubbed = QUOTED.sub(" ", text)
    hits = []
    for rx in RX:
        for m in rx.finditer(scrubbed):
            g = m.group(1) if m.groups() else m.group(0)
            hits.append((g or "").strip(" ,.?:;!\n"))
    seen, out = set(), []
    for h in hits:
        k = h.lower()
        if h and k not in seen:
            seen.add(k); out.append(h)
    return out


def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    data = json.loads(raw) if raw.strip() else {}
    if data.get("stop_hook_active"):
        return 0
    tp = data.get("transcript_path")
    if not tp or not os.path.exists(tp):
        return 0
    text = last_assistant_text(tp)
    if not text:
        return 0
    hits = find_decoration(text)

    def _beat(oc, reason=""):
        honesty_common.beat("honest_decoration_guard", oc, reason)

    if not hits:
        _beat("evaluated")
        return 0
    _beat("fired", "%d decoration phrase(s) blocked" % len(hits))
    log("BLOCK -- decoration found: %s" % ", ".join(hits[:8]))
    try:  # fail-safe salience tap -- a caught honesty-theatre slip for the dream (never raises)
        import hook_salience
        hook_salience.near_miss("honest_decoration_guard",
                                "caught honesty-as-decoration before it stood: " + ", ".join(hits[:4]),
                                ref="runtime/honest-decoration.log")
    except Exception:
        pass
    reason = (
        "HONESTY-DECORATION GUARD: your reply uses self-attestation of honesty as decoration -- "
        "the tic the operator has corrected 4x (feedback_drop_the_word_honest / "
        "feedback_honest_is_not_a_decoration). Found: " + "; ".join(repr(h) for h in hits[:8]) + ". "
        "Rule: don't CLAIM honesty -- it performs trustworthiness instead of being accurate, and "
        "'now do X honestly' brands the prior pass a lie. FIX: resend the SAME reply with these "
        "phrases deleted; state the thing plainly and let the accuracy stand. Mark the inverse "
        "(name uncertainty/inference where it exists) -- never advertise candor. (Discussing "
        "honesty, describing someone, asking the operator to be honest, or quoting the rule are all fine "
        "and won't trip this -- only the parenthetical garnish does.)"
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("fatal (fail-open): %s" % e)
        sys.exit(0)

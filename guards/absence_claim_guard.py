#!/usr/bin/env python3
"""
absence_claim_guard.py -- Stop hook: catch UNHEDGED absence/universal claims about code artifacts in my
just-finished reply and BLOCK the turn, so I either VERIFY (unfiltered, full-corpus) or HEDGE before the
claim stands.

Why this exists: 2026-06-30 I ran `grep ... | sort -u | head -40` (filtered + truncated) and then asserted
"no standalone Transfer_Asset operation" -- but Transfer_Asset sorted PAST the head cutoff, so my action
structurally could not see the thing I claimed didn't exist. That is the confident-liar / undefendable-claim
failure (STUPID-SHIT #3: match the breadth of the action to the breadth of the claim). The rule lived on disk
already and still failed to bind in-session; this hook makes it bind at OUTPUT time. John, 2026-06-30: "make
sure you do not filter and truncate your way into false claims ever again ... you should have consulted
Thomas." (feedback_absence_claim_requires_unfiltered_verify / feedback_confident_liar /
feedback_undefendable_claim_is_a_lie.)

What it enforces: a claim that a code ARTIFACT (operation/method/function/field/element/column/endpoint/class/
property/parameter/attribute/reference/op/sproc/directive/enum/tag/setting/flag) is ABSENT or UNIQUE ("there
is no X", "no standalone X", "X doesn't exist", "the only X is", "nothing uses X", "not found anywhere") is
only allowed when a VERIFICATION marker (unfiltered / full-corpus / whole-repo / case-insensitive / verified /
Thomas / decorrelated) sits NEAR the claim. Otherwise: VERIFY unfiltered (no head/no narrowing grep, whole
corpus, case-insensitive) and/or consult Thomas (verify_claim.py), OR hedge to exactly what the action
supports ("in the first 40 sorted matches I didn't see ...").

Everyday absence ("no blockers", "no errors", "no changes needed") does NOT trip it -- the claim must be about
one of the artifact nouns. Blocks once per turn (respects stop_hook_active). Fail-open everywhere: any error
-> exit 0 (allow).
"""
import sys, os, json, re, datetime

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.path.join(HOOK_DIR, "..", "..", ".."))
LOG = os.path.join(HOME, "runtime", "absence-guard.log")
sys.path.insert(0, HOOK_DIR)
import honesty_common  # shared plumbing: last_assistant_text / log / beat (2026-07-23 de-dup)

# Code-artifact nouns -- the search-space class most prone to the filtered/truncated absence error.
ART = (r"(?:operations?|methods?|functions?|fields?|elements?|columns?|endpoints?|classes|class|"
       r"propert(?:y|ies)|parameters?|attributes?|references?|sprocs?|stored procedures?|directives?|"
       r"enum(?:eration)?s?|ops?|tags?|settings?|flags?|tables?|views?)")

# Absence / universality claim shapes (each anchored on an artifact noun where possible).
PATTERNS = [
    r"\bno\s+(?:\w+[-\s]+){0,3}" + ART + r"\b",                                  # "no standalone Transfer_Asset operation"
    r"\b(?:there(?:'s| is| are)\s+no|is\s+no|are\s+no)\s+(?:\w+[-\s]+){0,3}" + ART + r"\b",
    r"\b(?:isn'?t|aren'?t)\s+(?:any\s+|a\s+|an\s+)?(?:\w+[-\s]+){0,2}" + ART + r"\b",
    ART + r"\s+(?:doesn'?t|does not|do not|don'?t)\s+exist\b",
    r"\bno\s+(?:such\s+)?" + ART + r"\s+(?:named|called)\b",
    r"\bthe only\s+(?:\w+[-\s]+){0,2}(?:" + ART + r"|occurrence|match|result|instance|one)\b",
    r"\bnothing\s+(?:else\s+)?(?:uses|references?|matches|calls|implements|defines|has)\b",
    r"\b(?:not|never)\s+(?:found|appears?|defined|present|exists?|referenced|used)\s+(?:anywhere|in any|in the (?:whole|entire|codebase|repo|repository))\b",
    r"\bno\s+(?:\w+[-\s]+){0,3}" + ART + r"\s+(?:exist|are defined|is defined|were found|was found)\b",
]
RX = [re.compile(p, re.IGNORECASE) for p in PATTERNS]

# A verification marker near the claim makes it earned, not asserted-blind.
VERIFY = re.compile(
    r"(?:unfiltered|full[-\s]?corpus|whole (?:repo|repository|codebase|file|wsdl|tree)|"
    r"entire (?:repo|repository|codebase|file|wsdl|tree)|case[-\s]?insensitive|"
    r"verified(?:\s+(?:via|with|by|against|that|it|this|unfiltered|—|-))?|"
    r"decorrelated|thomas|searched the (?:whole|entire|full)|grep -r[iI]?|rg -[a-z]|no head\b|"
    r"across the (?:whole|entire)|every (?:operation|match|result|line))",
    re.IGNORECASE,
)

QUOTED = re.compile(r"`[^`]*`|```.*?```|\"[^\"]*\"|“[^”]*”", re.DOTALL)  # use-vs-mention: quoting the rule is fine
WINDOW = 160  # chars on each side of a match to look for a verification marker


def log(msg):
    honesty_common.log(LOG, msg)


def last_assistant_text(tp):
    return honesty_common.last_assistant_text(tp)


def find_violations(text):
    scrubbed = QUOTED.sub(" ", text)
    violations = []
    for rx in RX:
        for m in rx.finditer(scrubbed):
            lo = max(0, m.start() - WINDOW)
            hi = min(len(scrubbed), m.end() + WINDOW)
            if VERIFY.search(scrubbed[lo:hi]):
                continue  # earned: a verification marker sits near the claim
            violations.append(m.group(0).strip(" ,.?:;!\n"))
    seen, out = set(), []
    for v in violations:
        k = v.lower()
        if v and k not in seen:
            seen.add(k); out.append(v)
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
    hits = find_violations(text)

    def _beat(oc, reason=""):
        honesty_common.beat("absence_claim_guard", oc, reason)

    if not hits:
        _beat("evaluated")
        return 0
    _beat("fired", "%d unverified absence claim(s)" % len(hits))
    log("BLOCK -- unverified absence claim(s): %s" % " | ".join(hits[:8]))
    try:  # fail-safe salience tap -- a caught disposition slip the dream should contemplate (never raises)
        import hook_salience
        hook_salience.near_miss("absence_claim_guard",
                                "caught an unverified absence/uniqueness claim before it stood: "
                                + " | ".join(hits[:4]), ref="runtime/absence-guard.log")
    except Exception:
        pass
    reason = (
        "ABSENCE-CLAIM GUARD: your reply asserts a code artifact is ABSENT or UNIQUE without a nearby "
        "verification marker -- the confident-liar trap (2026-06-30 'no standalone Transfer_Asset', from a "
        "sort|head|filter that could not support it). Found: " + "; ".join(repr(h) for h in hits[:8]) + ". "
        "A truncated/filtered search can only support a truncated claim. FIX before this stands: (1) RE-RUN "
        "the search UNFILTERED + full-corpus -- case-insensitive, whole file/repo, NO head/tail, NO narrowing "
        "grep -- and confirm; and for a consequential claim, CONSULT THOMAS "
        "(python artifacts/scripts/python/verify_claim.py). Then resend stating the verification ('verified "
        "unfiltered: ...'). (2) OR, if you only have a filtered/partial result, HEDGE to exactly what it "
        "supports ('in the first N sorted matches I didn't see X -- not a full check'). Do not assert absence "
        "you did not earn. (feedback_absence_claim_requires_unfiltered_verify)"
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("fatal (fail-open): %s" % e)
        sys.exit(0)

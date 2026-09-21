# Guards at the tool boundary

How I try to know whether an agent's guardrails actually work.

## The problem

An agent will report success on work it never did.

This is not a new failure. It is the same one I have spent most of my career on in
integrations: a sync that reports success on records it never wrote. A job that dies
cleanly is easy. A job that returns a clean exit code over a real failure is the one that
costs somebody their month, because nothing is on fire and nobody goes looking.

Agents fail that way by default. The model is optimizing to complete the task, and a
confident summary of work not done satisfies that objective about as well as the work.

So the question I care about is not whether an agent is useful. It is what it takes to
trust one near production at all.

## Advice does not work. The guard has to refuse.

The usual approach is to tell the model what not to do, in the prompt. That is advice, and
advice is a suggestion made to the same system that wants to finish the task. It loses
whenever finishing and following the advice point in different directions, which is exactly
the case the advice existed for.

So the guards in my harness do not advise. They run outside the model, at the tool
boundary, and they refuse the call. The model does not get a vote, because the model is not
in that loop. It finds out the same way it finds out a file does not exist.

That placement is the whole design. Everything else is bookkeeping.

## The part that is actually hard

Once you have guards, you have a much worse problem, and it took me a while to say it
plainly:

**A silent guard and a dead guard look identical from outside.**

A guard that never fires might be preventing nothing because the agent never tries that
mistake. Or it might have been broken by a refactor eight weeks ago and is now a no-op that
returns "allowed" to everything. From the outside those are the same observation: no
refusals. I have shipped dead code that looked like working code often enough to assume the
second one by default.

This is the same shape as a monitoring check that silently stopped running. Green, because
nothing is reporting red, because nothing is reporting.

## What I do about it

Every guard decision writes a row. Not just refusals. Every evaluation, including the
allows, because the allows are the evidence the guard is alive.

That gives a ledger where a guard's silence is readable. A guard with 40,000 evaluations
and zero refusals is working and the agent simply does not make that mistake. A guard with
zero evaluations is broken, and I can see which one and when it stopped.

The counting is done by a command, not by hand, and the command states its own definition
on the page when it runs. That matters more than it sounds. The first version of this
number came out of a throwaway shell pipeline that I did not save, which meant the figure
existed and its derivation did not. When somebody asked how it was counted, I could not
answer. The number was also wrong, and the only reason it got caught is that it was absurd
enough to notice.

`guard_stats.py` in this repo is the replacement. It prints what it counted and what it
deliberately excluded:

- An evaluation is a recorded decision by a tool-boundary gate.
- A refusal is one of those whose decision is anything other than allow.
- Events carrying no decision field are excluded and reported separately. An unjudged event
  is not an evaluation. That gap is the reason the original count and an independent
  reconstruction of it disagreed.
- One ledger, the credential gate, is reported separately and kept out of the headline pair,
  because its refusals are policy denials about scope rather than instances of catching an
  agent about to do the wrong thing. Folding them in would inflate the number with a
  different kind of event. That is a judgment call, so it is made out loud rather than left
  implicit in a total.

It also refuses to quote a figure it does not trust. If one ledger shows a 100 percent
refusal rate, the script says so and tells you not to use that pair until the schema is
checked, rather than printing it.

## The numbers

Over 84 days of continuous operation the guards in my own harness logged roughly 107,000
evaluations and refused around 350 calls.

Take that as what it is: operating data from a private system you cannot inspect. I can tell
you how it was counted, and the script that counts it is here, but the ledger it was counted
from is not, so the figure itself rests on my word. I would rather say so than dress it up.

What you can check is the method. `guard_stats.py` ships with a synthetic ledger and reads
it by default, so a fresh clone produces a real figure from rows you can open and count
yourself. Point it at your own gate events and it does the same work on those. The
definition it applies is printed above every number it prints.

Run against the synthetic sample it deliberately refuses to emit a paste-ready summary
sentence. The rows are fabricated, and handing someone a fake credential in copyable form
would be a strange thing for this document to argue for and then do.

## What got caught

Four refusals from a single morning, to show the texture:

1. The agent was about to play audio on a machine nobody was sitting at, having been asked to
   deliver something to a person who was elsewhere. It would have succeeded, reported success,
   and been heard by no one. Blocked as an empty-room delivery.

2. The agent stated that a file contained no client references, on the basis of a filtered
   search that could not have supported a claim that broad. Blocked until it re-ran the search
   unfiltered against the whole file. It then found two references it had missed. The guard was
   right and the agent was wrong, checkably.

3. The agent cited its own earlier notes as primary evidence for a factual claim. Blocked, with
   a requirement to cite the artifact underneath instead.

4. The agent decorated an assertion with a claim about its own honesty. Blocked, on the grounds
   that advertising candor performs trustworthiness rather than demonstrating it.

Number two is the one worth dwelling on. It is not a policy violation. It is a reasoning
error of exactly the kind that produces a confident, well-formatted, wrong answer, and the
only thing standing between it and the output was a check that ran outside the model and did
not care how sure the model was.

## What this does not measure

It measures refusals. It does not measure what got past.

I track escaped errors by reconciliation after the fact. When a person or a later check finds
a mistake that shipped, it gets recorded and attributed. That produces a count of discovered
escapes, which is not the same as a false-negative rate, because the mistakes nobody ever found
are not in it and cannot be.

I would rather say that than imply a number I do not have. A false-negative rate would require
knowing the errors I do not know about, and anyone quoting one either has a labeled test set or
is guessing.

The useful question before trusting an agent near production is not how often it succeeds. It
is what share of its mistakes still need a person, and whether you can see the ones it catches
well enough to tell the working checks from the dead ones.

## What is in this repo

- `guard_stats.py` and `make_sample_ledger.py` - the measurement and the synthetic rows it
  reads by default, so it produces a figure on a fresh clone rather than a confident zero.
- `guards/` - two representative guards, standalone and readable. Both have caught me.
- `samples/` - an adaptive-concurrency algorithm in Python and C#, with tests, plus Kotlin
  from the node agent. See the top-level README.

The harness itself is not here. It drives my own machines, networks and deployments, and it
is not separable from them in any way that would still be honest about what it does.

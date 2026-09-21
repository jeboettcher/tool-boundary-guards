<!-- EVIDENCE
AUDIENCE: EXTERNAL
22750 | PRIMARY: artifacts/public-staging/tool-boundary-guards/guard_stats.py | executed against the synthetic sample, judged rows
84 | PRIMARY: artifacts/public-staging/tool-boundary-guards/guard_stats.py | same execution, refusals and distinct days carrying events
765 | PRIMARY: artifacts/public-staging/tool-boundary-guards/make_sample_ledger.py | rows written with no decision field, excluded by the measurement
4 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/LaneService/Program.cs | launched --lanes 4, peak lanes busy reached exactly this
40 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | five consumer processes, eight records each, all completed
15.83 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | executed with the default connection limit, tasks per second
6.25 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | same workload with --max-connections 2, tasks per second
11 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | final adaptive limit, identical in both of those runs
60 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | records per run in that comparison
16 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/LaneService/Program.cs | lanes in that comparison
2.5 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | 15.83 divided by 6.25, the throughput ratio
two representative guards | PRIMARY: artifacts/public-staging/tool-boundary-guards/guards | the directory contains exactly two guard scripts
found two | PRIMARY: artifacts/public-staging/tool-boundary-guards/guards/absence_claim_guard.py | the re-run it forced surfaced two references the filtered search had missed
8 | NOT-A-CLAIM: a command-line argument value
5 | NOT-A-CLAIM: a command-line argument value
2 | NOT-A-CLAIM: a command-line argument value
300 | NOT-A-CLAIM: a command-line argument value
6 | NOT-A-CLAIM: a command-line argument value
-->

# tool-boundary-guards

Working samples from a private system that lets LLM agents operate real machines under
verification rather than on their own report.

The harness itself is not here. It drives my own machines, networks and deployments, and it
is not separable from them in any way that would still be honest about what it does. What
is here are the pieces that stand alone: an algorithm, a measurement, and two guards.

## About the code in this repository

I designed these. An agent wrote most of the code, under my direction, and I reviewed it.

That is worth stating plainly rather than leaving for someone to work out, because it is the
subject of the repository. `DESIGN.md` is the argument for why an agent operating real
systems needs guards that refuse at the tool boundary rather than advice in a prompt, and
the code here is what that arrangement produces. The parts I decided and the parts I
generated are different things, and a reader is entitled to ask which is which.

## What is here

### `DESIGN.md`
The argument. Why advice loses to a model optimising to finish, why the guard has to refuse
rather than warn, and the problem that turns out to be harder than building guards at all:
a silent guard and a dead guard look identical from outside.

### `guard_stats.py` and `sample-ledger/`
The measurement, with its counting definition printed alongside the number every time it
runs. It ships with a synthetic ledger so it produces a real figure on a fresh clone rather
than a confident zero.

```
python guard_stats.py                  # reads the synthetic sample
python guard_stats.py --ledger DIR     # a directory of real gate events
```

Against the sample it reports 22750 evaluations and 84 refusals across 84 days, having
excluded 765 rows that carried no decision at all. Those excluded rows are the point: an
unjudged event is not an evaluation, and conflating the two was the bug that made an earlier
version of this script disagree with an independent reconstruction of the same figure.

Run against the sample it refuses to print a paste-ready summary sentence, because the rows
are fabricated and handing someone a fake credential in copyable form would be a strange
thing for this repository to do.

### `guards/`
Two representative guards. Both refuse a call rather than advise against it, and both have
caught me.

- `absence_claim_guard.py` blocks a universal claim built on a filtered search. It once
  stopped me asserting a file contained no client references on the basis of a `grep` that
  could not have supported it. The re-run it forced found two.
- `honest_decoration_guard.py` blocks self-attestation of honesty used as decoration.
  Advertising candor performs trustworthiness instead of demonstrating it.

### `samples/adaptive-queue/` and `samples/adaptive-queue-csharp/`
The same algorithm in Python and C#: adaptive concurrency against an unknown ceiling shared
with processes that cannot see each other. No broker, no shared counter, no leader election.
Each instance knows only its own work, its own limit, and whether it was refused.

This is a clean-room rebuild of a design I used to rescue an ERP integration that had failed
under several previous contractors. That employer's code is not here and was not consulted.

The C# version carries something the Python one cannot. It exposes
`SocketsHttpHandler.MaxConnectionsPerServer`, so the second ceiling from the original story
becomes executable. Run the same workload twice:

| connection limit | final adaptive limit | refusals | throughput |
|---|---|---|---|
| .NET default | 11 | 0 | 15.83/sec |
| set to 2 | 11 | 0 | 6.25/sec |

Same limit, no refusals either time, and a 2.5 ratio between the throughputs. The controller
cannot see the constraint, so it reports the same healthy numbers while something underneath
it silently refuses to go faster. That is the failure the original rescue was about, and it
is the same shape as the guard problem: a constraint you cannot observe is indistinguishable
from no constraint at all.

## Running the queue samples

There is one service, in C#, and two clients that share it. Python first, needing nothing but
Python:

```
cd samples/adaptive-queue
python example_inprocess.py --capacity 9 --items 300   # no server required
python -m unittest discover -s tests                   # the regression suite
```

The service and the C# client, .NET 8, no packages:

```
cd samples/adaptive-queue-csharp
dotnet run --project LaneService -- --lanes 4          # one terminal
dotnet run --project Consumer -- --name w1 --records 8 # another
dotnet run --project Tests                             # the regression suite
```

Point the Python client at that same service and the two languages divide its lanes between them:

```
cd samples/adaptive-queue
python client.py --name py1 --records 6
```

## Licence

MIT. See `LICENSE`.

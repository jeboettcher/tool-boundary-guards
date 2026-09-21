<!-- EVIDENCE
AUDIENCE: EXTERNAL
9 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Tests/Program.cs | executed, all cases passing
nine | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Tests/Program.cs | the file defines nine runner.Case invocations
15.83 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | executed with the default connection limit, tasks per second
6.25 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | same workload with --max-connections 2, tasks per second
11 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | final adaptive limit, identical in both runs
60 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | records per run in that comparison
10 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | attempts per C# client in the mixed-language run
two | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/Consumer/Program.cs | the historical .NET Framework per-endpoint connection default, and the --max-connections value used
16 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/LaneService/Program.cs | lanes in that comparison
4 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/LaneService/Program.cs | launched --lanes 4, peak lanes busy reached exactly this
24 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue-csharp/LaneService/Program.cs | four mixed-language clients, six records each, all served
429 | NOT-A-CLAIM: HTTP status code
8 | NOT-A-CLAIM: a command-line argument value
-->

# NoCoordinatorQueue, C#

The service, the client, and the algorithm. .NET 8, no packages.

```
dotnet run --project LaneService -- --lanes 4      # one terminal
dotnet run --project Consumer -- --name w1 --records 8
dotnet run --project Tests                          # the test suite
```

`LaneService` is the far side for both this client and the Python one in
`../adaptive-queue/`. Start it once and point either at it.

## The projects

| project | what it is |
|---|---|
| `NoCoordinatorQueue` | the algorithm. Generic over the item and result types, so any async function can be handed in. |
| `LaneService` | a test API with a fixed number of lanes and no politeness. Refuses with 429 and publishes nothing else. |
| `Consumer` | one consumer process. Its only contribution to the algorithm is the refusal predicate. |
| `Tests` | the regression suite. Deliberately dependency-free: no xunit, no restore, nothing to install. |

## The second ceiling

This is what the C# version carries that the Python one cannot.

The design came from rescuing an ERP integration where the vendor capped concurrent
connections and .NET Framework's `ServicePointManager.DefaultConnectionLimit` silently
capped outbound connections per endpoint at two. Earlier attempts had scaled outward into
that second ceiling, adding servers that could never reach capacity already sitting unused.

On modern .NET that default is gone, but the class of bug is not. `Consumer` exposes
`SocketsHttpHandler.MaxConnectionsPerServer`, so you can reproduce the shape of it. Same
workload, 60 records, 16 lanes, run twice:

| connection limit | final adaptive limit | refusals | throughput |
|---|---|---|---|
| .NET default | 11 | 0 | 15.83/sec |
| set to 2 | 11 | 0 | 6.25/sec |

Same limit. No refusals either time. A 2.5 ratio in throughput.

By every signal the control loop can observe, those two runs are the same healthy run. The
constraint is below the layer that does the observing, so it produces no refusal to learn
from. That is the failure the original rescue was about, and it is the same shape as the
guard problem this repository opens with: a constraint you cannot observe is
indistinguishable from no constraint at all.

## Mixed-language fleet

Two C# clients and two Python clients against one `LaneService --lanes 4`, at the same time:

```
service: 24 served, peak lanes busy 4

cs1  completed=6 staged=0 attempts=10 refusals=4 finalLimit=2
cs2  completed=6 staged=0 attempts=10 refusals=4 finalLimit=2
py1  completed=6 staged=0 attempts=10 refusals=4 final_limit=2
py2  completed=6 staged=0 attempts=9  refusals=3 final_limit=2
```

Both implementations settled on the same limit with near-identical attempt counts. That
agreement is the point of maintaining two: a divergence between them is a bug in one.

## Tests

```
dotnet run --project Tests
```

Nine cases, exit code non-zero on the first failure. Each exists because the behaviour it
checks was once wrong: an item lost when the refusal predicate itself threw, a cancelled
run discarding every result it had collected, items processed in reverse of the input
order, tuning values accepted that defeat the control loop.

The accounting assertion is the cheapest of them: completed plus staged plus unattempted
equals the input, on every run including a cancelled one.

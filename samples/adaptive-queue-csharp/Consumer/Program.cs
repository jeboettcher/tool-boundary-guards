// One consumer process, driving NoCoordinatorQueue against the lane service.
//
//   dotnet run --project Consumer -- --name w1 --records 8
//   dotnet run --project Consumer -- --name w2 --records 8 --max-connections 2
//
// Run several against one LaneService. They divide its lanes between them without knowing
// how many lanes exist and without knowing about each other.
//
// ------------------------------------------------------------------------------------
// THE SECOND CEILING, which is the whole reason this sample is in C#
// ------------------------------------------------------------------------------------
// The original system this algorithm came from was .NET Framework, where
// ServicePointManager.DefaultConnectionLimit capped outbound connections per endpoint at
// TWO. Every rescue attempt before it had scaled outward -- more processes, more servers
// -- into a ceiling that had nothing to do with the vendor, and could never reach capacity
// that was sitting there unused.
//
// On modern .NET that particular default is gone: SocketsHttpHandler.MaxConnectionsPerServer
// defaults to unlimited. The CLASS of bug is not gone, it just moved. So --max-connections
// here sets it explicitly, and passing 2 reproduces the SHAPE of the historical ceiling.
// Not the mechanism: ServicePointManager scoped its limit per ServicePoint, this scopes per
// connection pool, and ASP.NET-hosted apps had different defaults again. The numbers below
// are what this code measured, not a re-enactment of .NET Framework.
//
// Run the fleet with --max-connections 2 and the adaptive limit will climb while
// throughput does not, because the limit is being honoured by a transport that cannot
// open the sockets to satisfy it. That is the failure mode, made visible: an adaptive
// controller reporting healthy numbers while something underneath it silently refuses to
// go faster.

using System.Diagnostics;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using NoCoordinator;

string name = Arg(args, "--name", "w1")!;
string url = Arg(args, "--url", "http://127.0.0.1:8099/work")!;
int records = ArgInt(args, "--records", 8);
int maxConnections = ArgInt(args, "--max-connections", 0);   // 0 = leave .NET's default
bool asJson = Array.IndexOf(args, "--json") >= 0;
string? configPath = Arg(args, "--config");

// Disallow rejects an unknown key instead of ignoring it. A misspelled knob that looks
// applied and is not is worse than no knob: you believe you changed the behaviour and
// you did not. The Python client enforces the same rule.
TuningConfig tuning = configPath is not null
    ? JsonSerializer.Deserialize<TuningConfig>(File.ReadAllText(configPath),
        new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        })!
    : new TuningConfig();

var handler = new SocketsHttpHandler();
if (maxConnections > 0)
{
    // The second ceiling, set deliberately. See the header comment.
    handler.MaxConnectionsPerServer = maxConnections;
}

using var http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(60) };
var rng = new Random(name.GetHashCode());
var items = Enumerable.Range(0, records)
    .Select(i => new WorkItem($"{name}-{i:D3}", rng.Next(1, 1000), rng.Next(1, 1000)))
    .ToList();

var clock = Stopwatch.StartNew();
IProgress<QueueEvent>? progress = asJson
    ? null
    : new Progress<QueueEvent>(e =>
        Console.WriteLine($"  [{e.Name}] {e.Kind,-8} limit={e.Limit} t={e.At.TotalSeconds,5:F1}s"));

var queue = new NoCoordinatorQueue<WorkItem, WorkResult>(
    work: async (item, ct) =>
    {
        using var resp = await http.PostAsJsonAsync(url, new { a = item.A, b = item.B }, ct);
        if ((int)resp.StatusCode == 429)
        {
            // The capacity signal. Nothing was attempted, so this is not a failure.
            throw new RefusedException();
        }
        resp.EnsureSuccessStatusCode();          // anything else IS a failure: staged
        var body = await resp.Content.ReadFromJsonAsync<WorkResponse>(cancellationToken: ct)
                   ?? throw new InvalidOperationException($"empty body for {item.Id}");
        if (body.Sum != item.A + item.B)
        {
            throw new InvalidOperationException(
                $"wrong answer for {item.Id}: expected {item.A + item.B}, got {body.Sum}");
        }
        return new WorkResult(item.Id, body.Sum, body.LaneMs);
    },
    tuning: tuning,
    progress: progress,
    name: name);

if (!asJson)
{
    Console.WriteLine($"{name}: {records} records -> {url}");
    Console.WriteLine(maxConnections > 0
        ? $"  MaxConnectionsPerServer = {maxConnections} (a ceiling underneath the algorithm)"
        : "  MaxConnectionsPerServer = .NET default (unlimited on modern .NET)");
}

var result = await queue.RunAsync(items);
clock.Stop();

if (asJson)
{
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        name,
        completed = result.Completed.Count,
        staged = result.Staged.Count,
        attempts = result.Attempts,
        refusals = result.Refusals,
        finalLimit = result.FinalLimit,
        elapsed = Math.Round(result.Elapsed.TotalSeconds, 2),
        throughput = Math.Round(result.Throughput, 2),
    }));
}
else
{
    Console.WriteLine();
    Console.WriteLine($"{name} done: completed={result.Completed.Count} " +
                      $"staged={result.Staged.Count} attempts={result.Attempts} " +
                      $"refusals={result.Refusals} finalLimit={result.FinalLimit} " +
                      $"elapsed={result.Elapsed.TotalSeconds:F1}s " +
                      $"throughput={result.Throughput:F2}/s");
    foreach (var s in result.Staged)
    {
        Console.WriteLine($"   staged {s.Item.Id}: {s.Error.Message}");
    }
}

static string? Arg(string[] argv, string flag, string? fallback = null)
{
    int i = Array.IndexOf(argv, flag);
    return i >= 0 && i + 1 < argv.Length ? argv[i + 1] : fallback;
}

static int ArgInt(string[] argv, string flag, int fallback)
{
    int i = Array.IndexOf(argv, flag);
    return i >= 0 && i + 1 < argv.Length && int.TryParse(argv[i + 1], out int v) ? v : fallback;
}

internal sealed record WorkItem(string Id, int A, int B);
internal sealed record WorkResult(string Id, int Sum, long LaneMs);
internal sealed record WorkResponse(int Sum, long LaneMs);

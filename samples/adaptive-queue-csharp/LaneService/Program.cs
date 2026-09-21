// The test API: a fixed number of lanes and no politeness.
//
//   dotnet run --project LaneService -- --lanes 4
//   dotnet run --project LaneService -- --lanes 8 --min-ms 200 --max-ms 1200
//
// POST /work  {"a":3,"b":4}
//   200  {"sum":7,"laneMs":513}   a lane was free and was held for that long
//   429  {"error":"no lane available"}
//
// The 429 is the ONLY signal a consumer ever gets. The lane count is never published,
// there is no queue, no Retry-After, and no hint about how much headroom is left or how
// many other consumers are competing. Everything the fleet does is inferred from refusals.
//
// GET /stats exists for whoever is watching the demo. A real vendor would not offer it and
// the consumers never call it.

using System.Diagnostics;

int lanes = ArgInt(args, "--lanes", 4);
int port = ArgInt(args, "--port", 8099);
int minMs = ArgInt(args, "--min-ms", 200);
int maxMs = ArgInt(args, "--max-ms", 1200);

var gate = new LaneGate(lanes);
var rng = new Random(ArgInt(args, "--seed", 11));
var rngLock = new object();

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.UseUrls($"http://127.0.0.1:{port}");
builder.Logging.ClearProviders();               // the demo output is the signal
var app = builder.Build();

app.MapPost("/work", async (WorkRequest req) =>
{
    if (!gate.TryAcquire())
    {
        // The whole protocol, right here.
        return Results.Json(new { error = "no lane available" }, statusCode: 429);
    }

    var sw = Stopwatch.StartNew();
    try
    {
        int hold;
        lock (rngLock) { hold = rng.Next(minMs, maxMs + 1); }
        await Task.Delay(hold);                 // the lane is occupied for this long
        return Results.Json(new { sum = req.A + req.B, laneMs = sw.ElapsedMilliseconds });
    }
    finally
    {
        gate.Release();
    }
});

app.MapGet("/stats", () => Results.Json(gate.Snapshot()));

Console.WriteLine($"test API on http://127.0.0.1:{port}");
Console.WriteLine($"  lanes         : {lanes}   (never published to consumers)");
Console.WriteLine($"  task duration : {minMs} to {maxMs} ms");
Console.WriteLine($"  theoretical   : {lanes / ((minMs + maxMs) / 2000.0):F2} tasks/sec");
Console.WriteLine();
app.Run();

static int ArgInt(string[] argv, string name, int fallback)
{
    int i = Array.IndexOf(argv, name);
    return i >= 0 && i + 1 < argv.Length && int.TryParse(argv[i + 1], out int v) ? v : fallback;
}

/// <summary>A fixed pool of workers. Occupied means occupied; there is no waiting room.</summary>
internal sealed class LaneGate(int count)
{
    private readonly object _lock = new();
    private int _busy;

    public int Served { get; private set; }
    public int Refused { get; private set; }
    public int PeakBusy { get; private set; }

    public bool TryAcquire()
    {
        lock (_lock)
        {
            if (_busy >= count) { Refused++; return false; }
            _busy++;
            if (_busy > PeakBusy) PeakBusy = _busy;
            return true;
        }
    }

    public void Release()
    {
        lock (_lock) { _busy--; Served++; }
    }

    public object Snapshot()
    {
        lock (_lock)
        {
            return new { lanes = count, busy = _busy, peakBusy = PeakBusy, served = Served, refused = Refused };
        }
    }
}

internal sealed record WorkRequest(int A, int B);

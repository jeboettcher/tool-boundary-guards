// Tests for NoCoordinatorQueue. Exits non-zero on the first failure.
//
//   dotnet run --project Tests
//
// Deliberately dependency-free: no xunit, no NuGet restore, nothing to install. The rest
// of this repository runs on a fresh clone with no setup and the tests should too.
//
// Each case here exists because the behaviour it checks was once wrong. They are
// regression tests before they are documentation.

using NoCoordinator;

var runner = new Runner();

// ---- accounting -----------------------------------------------------------------------

await runner.Case("every item is accounted for when all succeed", async () =>
{
    var q = new NoCoordinatorQueue<int, int>((i, _) => Task.FromResult(i * 2), tuning: Fast());
    var r = await q.RunAsync(Enumerable.Range(0, 50));
    Assert.Equal(50, r.Completed.Count, "completed");
    Assert.Equal(0, r.Staged.Count, "staged");
    Assert.Equal(50, r.AccountedFor, "accounted for");
});

await runner.Case("real failures are staged, never dropped", async () =>
{
    var q = new NoCoordinatorQueue<int, int>(
        (i, _) => i % 10 == 0 ? throw new InvalidOperationException("boom") : Task.FromResult(i),
        tuning: Fast());
    var r = await q.RunAsync(Enumerable.Range(0, 50));
    Assert.Equal(50, r.AccountedFor, "accounted for");
    Assert.Equal(5, r.Staged.Count, "staged");
});

await runner.Case("a refusal is retried and is not a failure", async () =>
{
    var seen = new Dictionary<int, int>();
    var gate = new object();
    var q = new NoCoordinatorQueue<int, int>((i, _) =>
    {
        lock (gate)
        {
            seen[i] = seen.GetValueOrDefault(i) + 1;
            if (seen[i] == 1) throw new RefusedException();
        }
        return Task.FromResult(i);
    }, tuning: Fast());

    var r = await q.RunAsync(Enumerable.Range(0, 20));
    Assert.Equal(20, r.Completed.Count, "completed");
    Assert.Equal(0, r.Staged.Count, "a refusal must never be recorded as a failure");
    Assert.Equal(20, r.Refusals, "refusals");
});

// ---- the refusal predicate ------------------------------------------------------------

await runner.Case("a throwing predicate cannot lose the item", async () =>
{
    var q = new NoCoordinatorQueue<int, int>(
        (i, _) => throw new InvalidOperationException("work failed"),
        isRefusal: _ => throw new ArgumentException("predicate is broken"),
        tuning: Fast());
    var r = await q.RunAsync(Enumerable.Range(0, 10));
    Assert.Equal(10, r.AccountedFor, "an item was lost");
    Assert.Equal(10, r.Staged.Count, "undecidable must mean 'not a refusal'");
});

// ---- cancellation ---------------------------------------------------------------------

await runner.Case("cancellation returns a partial result instead of throwing", async () =>
{
    using var cts = new CancellationTokenSource();
    var q = new NoCoordinatorQueue<int, int>(async (i, ct) =>
    {
        if (i == 5) cts.Cancel();
        await Task.Delay(5, ct);
        return i;
    }, tuning: Fast());

    var r = await q.RunAsync(Enumerable.Range(0, 200), cts.Token);
    Assert.Equal(200, r.AccountedFor, "completed + staged + unattempted must still equal the input");
    Assert.True(r.Unattempted.Count > 0, "a cancelled run should report unattempted work");
});

// ---- tuning ---------------------------------------------------------------------------

await runner.Case("nonsensical tuning is rejected", () =>
{
    foreach (double bad in new[] { 0.0, 1.0, 1.5, -0.5 })
    {
        Assert.Throws<ArgumentOutOfRangeException>(
            () => new TuningConfig { DecreaseFactor = bad }.Validate(),
            $"decrease factor {bad}");
    }
    Assert.Throws<ArgumentOutOfRangeException>(
        () => new TuningConfig { RetryFloorMs = 50, RetryCeilingMs = 5 }.Validate(),
        "ceiling below floor");
    return Task.CompletedTask;
});

// ---- the control loop -----------------------------------------------------------------

await runner.Case("the limit discovers a hidden ceiling and never exceeds it", async () =>
{
    const int capacity = 6;
    int busy = 0, peak = 0;
    var gate = new object();

    var q = new NoCoordinatorQueue<int, int>((i, _) =>
    {
        lock (gate)
        {
            if (busy >= capacity) throw new RefusedException();
            busy++;
            peak = Math.Max(peak, busy);
        }
        try { return Task.FromResult(i); }
        finally { lock (gate) { busy--; } }
    }, tuning: Fast());

    var r = await q.RunAsync(Enumerable.Range(0, 400));
    Assert.Equal(400, r.Completed.Count, "completed");
    Assert.True(peak <= capacity, $"peak {peak} exceeded the ceiling {capacity}");
});

await runner.Case("a transient failure does not move the limit", async () =>
{
    var q = new NoCoordinatorQueue<int, int>(
        (i, _) => throw new InvalidOperationException("transient"), tuning: Fast());
    int before = q.Limit;
    var r = await q.RunAsync(Enumerable.Range(0, 5));
    Assert.Equal(5, r.Staged.Count, "staged");
    Assert.Equal(before, q.Limit, "a real failure is not a capacity signal");
});

await runner.Case("items are processed in input order", async () =>
{
    var order = new List<int>();
    var gate = new object();
    var q = new NoCoordinatorQueue<int, int>((i, _) =>
    {
        lock (gate) { order.Add(i); }
        return Task.FromResult(i);
    }, tuning: Fast() with { StartLimit = 1, MaxLimit = 1 });

    await q.RunAsync(Enumerable.Range(0, 20));
    Assert.Equal(0, order[0], "first item processed first (matches the Python sibling)");
    Assert.Equal(19, order[^1], "last item processed last");
});

return runner.Summarise();


static TuningConfig Fast() =>
    new() { BackoffCooldownMs = 1, RetryFloorMs = 1, RetryCeilingMs = 2 };


/// <summary>Minimal case runner: names each case, stops counting nothing silently.</summary>
internal sealed class Runner
{
    private int _passed;
    private readonly List<string> _failures = [];

    public async Task Case(string name, Func<Task> body)
    {
        try
        {
            await body();
            _passed++;
            Console.WriteLine($"  ok    {name}");
        }
        catch (Exception ex)
        {
            _failures.Add($"{name}: {ex.Message}");
            Console.WriteLine($"  FAIL  {name}");
            Console.WriteLine($"        {ex.Message}");
        }
    }

    public int Summarise()
    {
        Console.WriteLine();
        Console.WriteLine($"{_passed} passed, {_failures.Count} failed");
        return _failures.Count == 0 ? 0 : 1;
    }
}

/// <summary>Assertions that say what they expected, so a failure is readable without a debugger.</summary>
internal static class Assert
{
    public static void Equal<T>(T expected, T actual, string what)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
            throw new Exception($"{what}: expected {expected}, got {actual}");
    }

    public static void True(bool condition, string what)
    {
        if (!condition) throw new Exception(what);
    }

    public static void Throws<TException>(Action action, string what) where TException : Exception
    {
        try { action(); }
        catch (TException) { return; }
        catch (Exception ex) { throw new Exception($"{what}: expected {typeof(TException).Name}, got {ex.GetType().Name}"); }
        throw new Exception($"{what}: expected {typeof(TException).Name}, nothing was thrown");
    }
}

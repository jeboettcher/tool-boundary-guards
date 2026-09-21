using System.Diagnostics;

namespace NoCoordinator;

/// <summary>
/// Signals that capacity was unavailable and <em>nothing was attempted</em>.
/// </summary>
/// <remarks>
/// This is the single most important distinction in the algorithm. A refusal is not a
/// failure: the item was never processed, so it returns to the queue untouched and is
/// counted as a failure nowhere. Anything else means the call happened and went wrong,
/// which is a real failure and gets staged with its error.
///
/// Classify a timeout or a 500 as a refusal and the fleet will throttle itself against
/// ordinary flakiness and never recover.
/// </remarks>
public sealed class RefusedException : Exception
{
    /// <param name="message">
    /// Optional detail for logs. The algorithm never inspects it: a refusal carries no
    /// information beyond having happened, which is the entire point. The far side does
    /// not tell you the cap, the headroom, or who else is competing for it.
    /// </param>
    public RefusedException(string message = "capacity unavailable") : base(message) { }
}

/// <summary>
/// The control-loop constants. Every one is an operational knob, not a compiled-in truth.
/// </summary>
/// <remarks>
/// The right curve is a property of the far side, not of the algorithm. A service that
/// refuses instantly and cheaply wants a gentler decrease than one that refuses slowly
/// under load; a service whose capacity swings through the day wants a shorter cooldown
/// than one with a fixed cap. Bind this from configuration rather than editing code.
/// </remarks>
public sealed record TuningConfig
{
    /// <summary>Initial concurrency. Start at 1 and let the loop discover the rest.</summary>
    public int StartLimit { get; init; } = 1;

    /// <summary>Hard stop, so a far side that never refuses cannot be flooded.</summary>
    public int MaxLimit { get; init; } = 64;

    /// <summary>Multiplicative decrease applied on refusal.</summary>
    public double DecreaseFactor { get; init; } = 0.5;

    /// <summary>
    /// Minimum milliseconds between two decreases. Refusals arriving inside this window
    /// are echoes of calls already in flight when the decision was made, not new
    /// information. Acting on each one compounds the cut and collapses the limit.
    /// </summary>
    /// <remarks>
    /// Milliseconds rather than TimeSpan so the same tuning file works for the Python
    /// client. TimeSpan serialises as "00:00:00.1", which Python will not read.
    /// </remarks>
    public int BackoffCooldownMs { get; init; } = 100;

    /// <summary>Lower bound, in milliseconds, of the jittered wait after a refusal.</summary>
    public int RetryFloorMs { get; init; } = 20;

    /// <summary>Upper bound, in milliseconds, of the jittered wait after a refusal.</summary>
    public int RetryCeilingMs { get; init; } = 80;

    /// <summary>Throws rather than silently accepting nonsense, because a tuning value
    /// that looks applied and is not is worse than no knob at all.</summary>
    public void Validate()
    {
        if (StartLimit < 1) throw new ArgumentOutOfRangeException(nameof(StartLimit));
        if (MaxLimit < StartLimit) throw new ArgumentOutOfRangeException(nameof(MaxLimit));
        if (DecreaseFactor is <= 0 or >= 1)
            throw new ArgumentOutOfRangeException(nameof(DecreaseFactor),
                "must be between 0 and 1 exclusive; 1 never decreases, above 1 would " +
                "INCREASE the limit on refusal, and 0 collapses straight to the floor");
        if (RetryCeilingMs < RetryFloorMs)
            throw new ArgumentOutOfRangeException(nameof(RetryCeilingMs), "ceiling is below floor");
        if (RetryFloorMs < 0)
            throw new ArgumentOutOfRangeException(nameof(RetryFloorMs), "cannot be negative");
        if (BackoffCooldownMs < 0)
            throw new ArgumentOutOfRangeException(nameof(BackoffCooldownMs), "cannot be negative");
    }
}

/// <summary>An item whose call was made and went wrong. Parked, never dropped.</summary>
public sealed record StagedItem<TItem>(TItem Item, Exception Error);

/// <summary>What one run produced.</summary>
/// <param name="Completed">Successful return values.</param>
/// <param name="Staged">Items whose call was made and failed. Never dropped.</param>
/// <param name="Unattempted">
/// Items never tried, which is only ever non-empty after cancellation. Without this the
/// identity below would silently break on a cancelled run and the caller could not tell
/// why.
/// </param>
/// <param name="Attempts">Calls made, including ones that were refused.</param>
/// <param name="Refusals">Capacity refusals. Not failures.</param>
/// <param name="FinalLimit">Concurrency the loop settled on.</param>
/// <param name="Elapsed">Wall clock for the run.</param>
public sealed record QueueRunResult<TItem, TResult>(
    IReadOnlyList<TResult> Completed,
    IReadOnlyList<StagedItem<TItem>> Staged,
    IReadOnlyList<TItem> Unattempted,
    int Attempts,
    int Refusals,
    int FinalLimit,
    TimeSpan Elapsed)
{
    /// <summary>
    /// Completed + Staged + Unattempted == input, always. The cheapest correctness check
    /// available, and worth asserting in tests.
    /// </summary>
    public int AccountedFor => Completed.Count + Staged.Count + Unattempted.Count;

    /// <summary>
    /// Completed items per second over the whole run. Counts only successes: staged
    /// failures and refusals consumed wall clock but delivered nothing, and folding them
    /// in would flatter a run that was mostly bouncing off the ceiling.
    /// </summary>
    public double Throughput => Elapsed.TotalSeconds > 0
        ? Completed.Count / Elapsed.TotalSeconds
        : 0d;
}

/// <summary>Emitted when the control loop moves, for logging or a live display.</summary>
public sealed record QueueEvent(string Name, string Kind, int Limit, TimeSpan At);

/// <summary>
/// Adaptive concurrency against an unknown ceiling shared with instances you cannot see.
/// </summary>
/// <remarks>
/// <para>
/// Give it any asynchronous function and a way to recognise a refusal. It runs that
/// function as concurrently as the far side tolerates, discovers the ceiling from refusals
/// alone, and divides that ceiling with other instances without exchanging a byte with
/// them. There is no broker, no shared counter, no leader election and no discovery: an
/// instance knows only its own items, its own limit, and whether its last calls were
/// refused.
/// </para>
/// <para>
/// The loop is additive-increase / multiplicative-decrease. AIMD is what lets independent
/// competitors converge on a roughly fair share of a contended resource without
/// communicating, which is precisely the situation when the instances cannot see each
/// other.
/// </para>
/// </remarks>
/// <typeparam name="TItem">The unit of work handed in.</typeparam>
/// <typeparam name="TResult">Whatever the work function returns on success.</typeparam>
public sealed class NoCoordinatorQueue<TItem, TResult>
{
    private readonly Func<TItem, CancellationToken, Task<TResult>> _work;
    private readonly Func<Exception, bool> _isRefusal;
    private readonly TuningConfig _tuning;
    private readonly IProgress<QueueEvent>? _progress;
    private readonly string _name;
    private readonly Random _rng;
    private readonly object _gate = new();

    private double _limit;
    private long _lastBackoffTicks;

    /// <param name="work">
    /// The call to make for each item. Throw a refusal from here when the far side says
    /// it has no capacity; throw anything else when the call happened and went wrong.
    /// </param>
    /// <param name="isRefusal">
    /// Classifies an exception as a capacity refusal. Defaults to
    /// <see cref="RefusedException"/>. This predicate is the contract: get it wrong and
    /// the algorithm breaks in the worst direction, throttling itself against ordinary
    /// flakiness until it never recovers. A timeout is usually not a refusal.
    /// </param>
    /// <param name="tuning">Control-loop constants. Bind from configuration.</param>
    /// <param name="progress">Optional sink for limit movements, for logs or a display.</param>
    /// <param name="name">Identifies this instance in emitted events. Not coordination.</param>
    /// <param name="seed">
    /// Fixes the retry jitter for reproducible tests. Leave null in production: the jitter
    /// exists so independent instances do not synchronise into retry waves, and a shared
    /// seed across a fleet would defeat it.
    /// </param>
    public NoCoordinatorQueue(
        Func<TItem, CancellationToken, Task<TResult>> work,
        Func<Exception, bool>? isRefusal = null,
        TuningConfig? tuning = null,
        IProgress<QueueEvent>? progress = null,
        string name = "q",
        int? seed = null)
    {
        _work = work ?? throw new ArgumentNullException(nameof(work));
        _isRefusal = isRefusal ?? (ex => ex is RefusedException);
        _tuning = tuning ?? new TuningConfig();
        _tuning.Validate();
        _progress = progress;
        _name = name;
        // Not name.GetHashCode(): .NET randomises string hashing per process, so that seed
        // differs between runs of the same named instance. This one is stable, which keeps
        // fleet members' jitter uncorrelated AND a single instance reproducible.
        _rng = seed is { } s ? new Random(s) : new Random(StableSeed(name));
        _limit = _tuning.StartLimit;
    }

    /// <summary>Current concurrency limit. Moves as the far side responds.</summary>
    public int Limit { get { lock (_gate) return (int)_limit; } }

    /// <summary>FNV-1a over the name. Deterministic across processes, unlike string hashing.</summary>
    private static int StableSeed(string name)
    {
        unchecked
        {
            uint hash = 2166136261;
            foreach (char c in name) { hash ^= c; hash *= 16777619; }
            return (int)(hash & 0x7FFFFFFF);
        }
    }

    /// <summary>
    /// Additive increase, one slot per round trip: the limit rises by 1/limit per success.
    /// </summary>
    /// <remarks>
    /// The rejected alternative is a flat +1 per success. That lets the limit outrun the
    /// refusals pulling it down, and the fleet parks permanently above the ceiling: it was
    /// measured at limits summing to 54 against a cap of 12, with 44 refusals for every
    /// record served. Scaling the step by 1/limit makes a gain cost `limit` successes, so
    /// the climb slows exactly as the limit grows.
    /// </remarks>
    private void Increase()
    {
        lock (_gate)
        {
            if (_limit < _tuning.MaxLimit)
                _limit = Math.Min(_tuning.MaxLimit, _limit + (1.0 / _limit));
        }
    }

    /// <summary>Multiplicative decrease, at most once per cooldown window.</summary>
    /// <returns>
    /// True only if the limit actually changed. At the floor of 1 the cooldown is still
    /// consumed but nothing moves, and reporting that as a backoff would put an event in
    /// the log for a decision that had no effect.
    /// </returns>
    private bool Decrease()
    {
        lock (_gate)
        {
            long now = Stopwatch.GetTimestamp();
            double sinceMs = (now - _lastBackoffTicks) * 1000.0 / Stopwatch.Frequency;

            // Explicit sentinel rather than relying on the arithmetic: _lastBackoffTicks
            // starts at 0, which would make sinceMs enormous and pass by accident.
            if (_lastBackoffTicks != 0 && sinceMs < _tuning.BackoffCooldownMs)
                return false;                       // an echo, not new information

            _lastBackoffTicks = now;
            double before = _limit;
            _limit = Math.Max(1.0, _limit * _tuning.DecreaseFactor);
            return _limit < before;
        }
    }

    /// <summary>Random is not thread-safe, hence the lock.</summary>
    private TimeSpan JitteredRetryDelay()
    {
        lock (_gate)
        {
            int floor = _tuning.RetryFloorMs;
            int ceiling = _tuning.RetryCeilingMs;
            return TimeSpan.FromMilliseconds(floor + (_rng.NextDouble() * (ceiling - floor)));
        }
    }

    /// <summary>
    /// Works through <paramref name="items"/>, discovering how much concurrency the far
    /// side allows and holding there.
    /// </summary>
    /// <param name="items">
    /// Enumerated once, up front. Refused items are pushed back and retried, so the number
    /// of attempts exceeds the number of items whenever the ceiling is contended.
    /// </param>
    /// <param name="ct">
    /// Cancellation is passed through to the work function and to the retry delay, so
    /// in-flight calls ARE cancelled rather than awaited. Cancelling therefore abandons
    /// work the far side may already have performed, and the returned counts will not
    /// account for it.
    /// <para>
    /// Prefer letting a run drain. If you must cancel, treat the result as a lower bound
    /// on what happened remotely, not as a record of it.
    /// </para>
    /// </param>
    /// <returns>
    /// Successes, staged failures, and the counters. Completed plus staged always equals
    /// the input on an uncancelled run; that identity is the cheapest correctness check
    /// available and is worth asserting in tests.
    /// </returns>
    public async Task<QueueRunResult<TItem, TResult>> RunAsync(
        IEnumerable<TItem> items, CancellationToken ct = default)
    {
        // Reversed so the stack pops in input order, matching the Python sibling.
        var pending = new Stack<TItem>(items.Reverse());
        var completed = new List<TResult>();
        var staged = new List<StagedItem<TItem>>();
        var running = new List<Task>();
        int attempts = 0, refusals = 0;
        var started = Stopwatch.StartNew();

        async Task AttemptAsync(TItem item)
        {
            try
            {
                TResult value = await _work(item, ct).ConfigureAwait(false);
                lock (_gate) { completed.Add(value); }
                Increase();
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                // Cancellation is neither a refusal nor a failure. The call may or may not
                // have reached the far side, so the only honest record is "not attempted".
                lock (_gate) { pending.Push(item); }
            }
            catch (Exception ex) when (IsRefusalSafely(ex))
            {
                lock (_gate) { refusals++; pending.Push(item); }   // untouched
                if (Decrease())
                    Report("backoff", started.Elapsed);

                // Back off in time as well as concurrency: the remaining slots would
                // otherwise just cycle faster. Jittered so a fleet does not synchronise.
                try
                {
                    await Task.Delay(JitteredRetryDelay(), ct).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    // Swallowed on purpose. Throwing here faults this task, and the
                    // Task.WhenAll below would then discard every result collected so far.
                }
            }
            catch (Exception ex)
            {
                lock (_gate) { staged.Add(new StagedItem<TItem>(item, ex)); }
                Report("staged", started.Elapsed);
                // Deliberately NOT a capacity signal: the limit does not move.
            }
        }

        // A predicate that throws cannot be allowed to decide. It is not asserting
        // "capacity unavailable", so the safe reading is "not a refusal": the item gets
        // staged and a human sees it.
        bool IsRefusalSafely(Exception ex)
        {
            try { return _isRefusal(ex); }
            catch { return false; }
        }

        while (!ct.IsCancellationRequested)
        {
            running.RemoveAll(t => t.IsCompleted);

            TItem? next = default;
            bool haveItem = false;
            int limitNow;
            lock (_gate)
            {
                limitNow = (int)_limit;
                if (pending.Count > 0 && running.Count < limitNow)
                {
                    next = pending.Pop();
                    haveItem = true;
                    attempts++;
                }
            }

            if (haveItem)
            {
                running.Add(AttemptAsync(next!));
                continue;
            }

            if (running.Count == 0)
            {
                lock (_gate) { if (pending.Count == 0) break; }
                // Pending work but no slots and nothing running should be impossible;
                // yielding rather than spinning keeps it from becoming a hot loop if it is.
                await Task.Delay(5, ct).ConfigureAwait(false);
                continue;
            }

            // At the limit, or out of items with work still in flight. Wait for the first
            // completion rather than polling, so the loop costs nothing while it waits.
            await Task.WhenAny(running).ConfigureAwait(false);
        }

        // Observe every task without letting one fault destroy the run. Results are
        // already recorded under the lock; an exception escaping here would throw them
        // away and hand the caller nothing.
        foreach (var t in running)
        {
            try { await t.ConfigureAwait(false); }
            catch { /* recorded already, or a cancellation we chose to absorb */ }
        }
        started.Stop();

        TItem[] unattempted;
        lock (_gate) { unattempted = pending.ToArray(); }

        return new QueueRunResult<TItem, TResult>(
            completed, staged, unattempted, attempts, refusals, Limit, started.Elapsed);
    }

    private void Report(string kind, TimeSpan at) =>
        _progress?.Report(new QueueEvent(_name, kind, Limit, at));
}

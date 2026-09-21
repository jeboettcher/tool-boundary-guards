"""NoCoordinatorQueue -- adaptive concurrency against an unknown, shared ceiling.

Hand it any callable and a way to recognise a refusal. It runs that callable as
concurrently as the far side will tolerate, discovering the ceiling from refusals alone,
while sharing that ceiling with other instances it cannot see and does not know about.

    from adaptive import NoCoordinatorQueue, Refused

    def charge(order):
        r = requests.post(VENDOR, json=order)
        if r.status_code == 429:
            raise Refused()          # capacity signal
        r.raise_for_status()         # anything else is a real failure
        return r.json()

    q = NoCoordinatorQueue(charge)
    result = q.run(orders)

    result.completed   -> list of return values
    result.staged      -> [{"item": ..., "error": ...}] failed, never dropped
    result.stats()     -> completed, staged, attempts, refusals, final_limit,
                          elapsed, throughput

WHAT MAKES IT WORK
No coordinator. No broker, no shared counter, no leader election, no service discovery.
An instance knows only its own items, its own concurrency limit, and whether its last calls
were refused. Run ten of these in ten processes on ten machines and they will divide the
far side's capacity between them without exchanging a byte.

The control loop is additive-increase / multiplicative-decrease. AIMD is what lets
independent competitors converge on a roughly fair share of a contended resource without
communicating, which is precisely the situation here.

THE REFUSAL PREDICATE IS THE CONTRACT
A refusal means "capacity was unavailable, nothing was attempted". It is not a failure, and
the item goes back to be retried untouched. Anything else is a real failure: the call
happened and went wrong, so the item is staged with its error rather than dropped.

Getting this boundary wrong is the most common way to break the algorithm. A timeout is
usually NOT a refusal. A 500 is usually NOT a refusal. Treat ordinary flakiness as a
capacity signal and the fleet throttles itself to the floor and never recovers.

Standard library only. Thread-based, so it suits IO-bound work, which is what a remote
capacity ceiling implies.
"""

import random
import threading
import time


class Refused(Exception):
    """Capacity was unavailable. Nothing was attempted."""


def _stable_seed(name):
    """FNV-1a over the name. Deterministic across processes, unlike hash()."""
    h = 2166136261
    for ch in name:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h & 0x7FFFFFFF


# Every constant in the control loop is an operational knob, not a compiled-in truth.
# The right curve is a property of the far side, not of the algorithm: a vendor that
# refuses instantly and cheaply wants a gentler decrease than one that refuses slowly
# under load, and a vendor whose capacity swings through the day wants a shorter cooldown
# than one with a fixed cap. These are the defaults, meant to be overridden per
# integration, from a config file, environment, or whatever your deployment already uses.
# Milliseconds, not seconds, so the C# client reads the same tuning file.
DEFAULT_TUNING = {
    "start_limit": 1,           # always start at 1 and discover upward
    "max_limit": 64,            # hard stop, so a far side with no limit cannot be flooded
    "decrease_factor": 0.5,     # multiplicative decrease applied on refusal
    "backoff_cooldown_ms": 100, # min gap between two decreases
    "retry_floor_ms": 20,       # jittered wait after a refusal, lower bound
    "retry_ceiling_ms": 80,     # ...and upper bound
}


class Result:
    def __init__(self):
        self.completed = []
        self.staged = []
        self._lock = threading.Lock()
        self.attempts = 0
        self.refusals = 0
        self.elapsed = 0.0
        self.final_limit = 0

    def _complete(self, value):
        with self._lock:
            self.completed.append(value)

    def _stage(self, item, error):
        with self._lock:
            self.staged.append({"item": item, "error": error})

    def stats(self):
        return {
            "completed": len(self.completed),
            "staged": len(self.staged),
            "attempts": self.attempts,
            "refusals": self.refusals,
            "final_limit": self.final_limit,
            "elapsed": round(self.elapsed, 2),
            "throughput": round(len(self.completed) / self.elapsed, 2) if self.elapsed else 0.0,
        }


class NoCoordinatorQueue:
    def __init__(self, work_fn, is_refusal=None, start_limit=None, max_limit=None,
                 decrease_factor=None, backoff_cooldown_ms=None,
                 retry_floor_ms=None, retry_ceiling_ms=None,
                 on_event=None, name="q", seed=None):
        """
        work_fn(item)      the thing to do. Its return value lands in result.completed.
        is_refusal(exc)    True if this exception means "no capacity, nothing attempted".
                           Defaults to isinstance(exc, Refused).
        start_limit        initial concurrency. Start at 1 and let it discover the rest.
        max_limit          hard ceiling, so a far side with no limit cannot be flooded.
        decrease_factor    multiplicative decrease applied on refusal.
        backoff_cooldown_ms  minimum gap between two decreases. Refusals inside this
                           window are echoes of calls already in flight when the decision
                           was made, not new information.
        retry_floor_ms /   jittered wait after a refusal. Backing off in concurrency alone
        retry_ceiling_ms   is not enough; the remaining slots simply cycle faster.
        on_event(dict)     optional observability hook. Called with {"event": ...}.
        name               identifies this instance in events. Not coordination.
        seed               fixes the jitter for tests. Leave None in production: the jitter
                           exists so instances do not synchronise, and a shared seed across
                           a fleet would defeat it.
        """
        # One source of truth for the tuning constants: DEFAULT_TUNING. An explicit
        # argument wins; None means "use the configured default".
        def tuned(value, key):
            return DEFAULT_TUNING[key] if value is None else value

        self.work_fn = work_fn
        self.is_refusal = is_refusal or (lambda exc: isinstance(exc, Refused))
        self.limit = float(tuned(start_limit, "start_limit"))
        self.max_limit = float(tuned(max_limit, "max_limit"))
        self.decrease_factor = tuned(decrease_factor, "decrease_factor")
        self.backoff_cooldown_ms = tuned(backoff_cooldown_ms, "backoff_cooldown_ms")
        self.retry_floor_ms = tuned(retry_floor_ms, "retry_floor_ms")
        self.retry_ceiling_ms = tuned(retry_ceiling_ms, "retry_ceiling_ms")
        self.on_event = on_event
        self.name = name

        # Not hash(name): Python randomises string hashing per process (PYTHONHASHSEED),
        # so that seed differs between runs. This one is stable, which keeps fleet members'
        # jitter uncorrelated AND a single instance reproducible. Matches the C# sibling.
        self._rng = random.Random(seed if seed is not None else _stable_seed(name))

        # limit and in-flight share one lock, so the gate and the number it gates on can
        # never disagree.
        self._cv = threading.Condition()
        self._in_flight = 0
        self._last_backoff = 0.0
        self._stop = threading.Event()
        self._validate()

    def _validate(self):
        """Reject tuning that cannot work, rather than behaving oddly with it.

        `from_config` checks that the KEYS are real; this checks the VALUES. Both halves
        are needed: a typo'd key and a nonsensical value both leave you believing you
        tuned something you did not. The C# sibling validates the same conditions.
        """
        if self.limit < 1:
            raise ValueError("start_limit must be at least 1")
        if self.max_limit < self.limit:
            raise ValueError("max_limit is below start_limit")
        if not 0 < self.decrease_factor < 1:
            raise ValueError(
                "decrease_factor must be between 0 and 1 exclusive; 1 never decreases, "
                "above 1 would INCREASE the limit on refusal, and 0 collapses to the floor")
        if self.retry_ceiling_ms < self.retry_floor_ms:
            raise ValueError("retry_ceiling_ms is below retry_floor_ms")
        if self.retry_floor_ms < 0:
            raise ValueError("retry_floor_ms cannot be negative")
        if self.backoff_cooldown_ms < 0:
            raise ValueError("backoff_cooldown_ms cannot be negative")

    @classmethod
    def from_config(cls, work_fn, config=None, **overrides):
        """Build from a config mapping, so the curve is deployed rather than compiled.

            import json
            tuning = json.load(open("tuning.json"))
            q = NoCoordinatorQueue.from_config(charge, tuning, name="w1")

        Unknown keys are rejected rather than silently ignored, because a tuning constant
        that looks applied and is not is the worst of both: you believe you changed the
        behaviour and you did not.
        """
        merged = dict(DEFAULT_TUNING)
        for key, value in (config or {}).items():
            if key not in DEFAULT_TUNING:
                raise ValueError(
                    "unknown tuning key %r; expected one of %s"
                    % (key, ", ".join(sorted(DEFAULT_TUNING))))
            merged[key] = value
        merged.update(overrides)
        return cls(work_fn, **merged)

    # -- the control loop -------------------------------------------------------------

    def _emit(self, **kw):
        if self.on_event:
            try:
                self.on_event(dict(kw, name=self.name, limit=int(self.limit)))
            except Exception:
                pass                      # observability must never break the work

    def _acquire(self):
        with self._cv:
            while self._in_flight >= int(self.limit):
                if self._stop.is_set():
                    return False
                self._cv.wait(timeout=0.05)
            self._in_flight += 1
            return True

    def _release(self):
        with self._cv:
            self._in_flight -= 1
            self._cv.notify()

    def _increase(self):
        """Additive increase, one slot per round trip: +1/limit per success.

        The rejected alternative is a flat +1 per success. That lets the limit outrun the
        refusals pulling it down and the fleet parks permanently above the ceiling:
        measured at limits summing to 54 against a cap of 12. Scaling the step by 1/limit
        makes a gain cost `limit` successes, so the climb slows as the limit grows.
        """
        with self._cv:
            if self.limit < self.max_limit:
                self.limit = min(self.max_limit, self.limit + (1.0 / self.limit))

    def _decrease(self):
        """Multiplicative decrease, at most once per cooldown window.

        Returns True only if the limit actually moved. At the floor of 1 the cooldown is
        still consumed but nothing changes, and reporting that as a backoff would log an
        event for a decision that had no effect.
        """
        now = time.monotonic()
        with self._cv:
            # `_last_backoff` starts at 0.0 and time.monotonic()'s origin is undefined --
            # on some platforms it counts from boot, so on a freshly started machine
            # `now - 0.0` can be SMALLER than the cooldown and swallow the very first
            # decrease. The explicit sentinel check removes that dependency.
            since_ms = (now - self._last_backoff) * 1000.0
            if self._last_backoff != 0.0 and since_ms < self.backoff_cooldown_ms:
                return False
            self._last_backoff = now
            before = self.limit
            self.limit = max(1.0, self.limit * self.decrease_factor)
            self._cv.notify_all()
            return self.limit < before

    # -- running ----------------------------------------------------------------------

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    def run(self, items):
        pending = list(items)
        pending.reverse()                 # pop() from the end
        lock = threading.Lock()
        result = Result()
        started = time.monotonic()
        threads = []

        def attempt(item):
            try:
                with lock:
                    result.attempts += 1
                value = self.work_fn(item)
                result._complete(value)
                self._increase()
            except Exception as exc:
                # Classify OUTSIDE the branch, and never let the predicate take the item
                # with it. A caller-supplied is_refusal that raises used to kill this
                # thread inside the except block, leaving the item in neither `pending`,
                # `completed` nor `staged` -- silently lost, which is the one outcome this
                # class exists to prevent. A predicate that cannot decide is not asserting
                # "capacity unavailable", so the safe reading is "not a refusal": the item
                # gets staged and a human sees it.
                try:
                    refused = self.is_refusal(exc)
                except Exception as predicate_error:
                    refused = False
                    exc = RuntimeError(
                        "is_refusal(%r) raised %r; treated as a real failure" % (exc, predicate_error))

                if refused:
                    with lock:
                        result.refusals += 1
                        pending.append(item)      # untouched; nothing was attempted
                    if self._decrease():
                        self._emit(event="backoff")
                    # Back off in time as well as in concurrency, jittered so a fleet does
                    # not synchronise into retry waves.
                    time.sleep(self._rng.uniform(
                        self.retry_floor_ms, self.retry_ceiling_ms) / 1000.0)
                else:
                    result._stage(item, repr(exc))  # real failure: parked, not dropped
                    self._emit(event="staged")
            except BaseException as fatal:
                # KeyboardInterrupt / SystemExit are not Exception subclasses and would
                # otherwise unwind past the handler above, taking the item with them.
                # Stage it and re-raise: the item is accounted for either way.
                result._stage(item, repr(fatal))
                raise
            finally:
                self._release()

        while not self._stop.is_set():
            with lock:
                if not pending and not any(t.is_alive() for t in threads):
                    break
            if not self._acquire():
                break
            with lock:
                item = pending.pop() if pending else None
            if item is None:
                self._release()
                threads = [t for t in threads if t.is_alive()]
                if not threads:
                    break
                time.sleep(0.01)
                continue
            t = threading.Thread(target=attempt, args=(item,), daemon=True)
            threads.append(t)
            t.start()
            threads = [t for t in threads if t.is_alive()]

        # Join WITHOUT a timeout. An earlier version used join(timeout=30) and then
        # returned regardless, which meant a slow work_fn produced a Result that daemon
        # threads went on mutating after run() had handed it to the caller: counts that
        # change while you read them, and `completed + staged == input` true or false
        # depending on when you looked. If a work_fn never returns, this hangs -- the same
        # contract the C# sibling has, and a hang is at least honest about being stuck.
        for t in threads:
            t.join()

        result.elapsed = time.monotonic() - started
        result.final_limit = int(self.limit)
        return result

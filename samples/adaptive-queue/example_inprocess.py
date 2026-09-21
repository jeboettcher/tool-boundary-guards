"""The same NoCoordinatorQueue, against a plain Python function instead of HTTP.

    python example_inprocess.py
    python example_inprocess.py --capacity 12 --items 400

This exists to show that the algorithm does not know or care what it is calling. The HTTP
client raises Refused on a 429; this raises it when an in-memory semaphore is full. The
control loop is identical and untouched.

It also runs in a couple of seconds, which the 1-to-8-second lane service does not.
"""

import argparse
import random
import threading
import time

from adaptive import NoCoordinatorQueue, Refused


class CappedResource:
    """Something with a hard concurrency limit that it refuses to tell you about."""

    def __init__(self, capacity, min_s, max_s, failure_rate, seed=None):
        self.capacity = capacity
        self.min_s, self.max_s = min_s, max_s
        self.failure_rate = failure_rate
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._busy = 0
        self.peak_busy = 0
        self.served = 0

    def __call__(self, item):
        with self._lock:
            if self._busy >= self.capacity:
                raise Refused("at capacity")
            self._busy += 1
            self.peak_busy = max(self.peak_busy, self._busy)
            hold = self._rng.uniform(self.min_s, self.max_s)
            flaky = self._rng.random() < self.failure_rate
        try:
            time.sleep(hold)
            if flaky:
                # Not a capacity signal. If the queue treated this as a refusal it would
                # throttle itself against ordinary flakiness and never recover.
                raise RuntimeError("transient failure on %s" % item)
            with self._lock:
                self.served += 1
            return item
        finally:
            with self._lock:
                self._busy -= 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--capacity", type=int, default=9, help="hidden from the queue")
    p.add_argument("--items", type=int, default=300)
    p.add_argument("--min-seconds", type=float, default=0.04)
    p.add_argument("--max-seconds", type=float, default=0.12)
    p.add_argument("--failure-rate", type=float, default=0.02)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    resource = CappedResource(args.capacity, args.min_seconds, args.max_seconds,
                              args.failure_rate, seed=args.seed)
    items = ["item-%04d" % i for i in range(args.items)]

    print("hidden capacity : %d  (the queue is NOT told)" % args.capacity)
    q = NoCoordinatorQueue(resource, max_limit=64, name="inproc", seed=args.seed)
    result = q.run(items)
    stats = result.stats()

    print("completed       : %d" % stats["completed"])
    print("staged          : %d   (failed, never dropped)" % stats["staged"])
    print("accounted for   : %d of %d" % (stats["completed"] + stats["staged"], args.items))
    print("refusals        : %d" % stats["refusals"])
    print("final limit     : %d" % stats["final_limit"])
    print("peak busy seen  : %d   <- found by touching the ceiling" % resource.peak_busy)
    print("elapsed         : %.2fs  (%.0f items/sec)" % (stats["elapsed"], stats["throughput"]))
    if result.staged:
        print("\nstaged for reprocessing:")
        for s in result.staged[:5]:
            print("   %s  %s" % (s["item"], s["error"]))
        if len(result.staged) > 5:
            print("   ... and %d more" % (len(result.staged) - 5))


if __name__ == "__main__":
    main()

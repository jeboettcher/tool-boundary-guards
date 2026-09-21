"""Tests for NoCoordinatorQueue. Standard library unittest, no dependencies.

    python -m unittest discover -s tests -v      (from samples/adaptive-queue)

Each test here exists because the behaviour it checks was once wrong. They are regression
tests before they are documentation.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adaptive import DEFAULT_TUNING, NoCoordinatorQueue, Refused, _stable_seed


FAST = {"backoff_cooldown_ms": 1, "retry_floor_ms": 1, "retry_ceiling_ms": 2}


class Accounting(unittest.TestCase):
    """completed + staged == input. The cheapest correctness check there is."""

    def test_all_succeed(self):
        q = NoCoordinatorQueue.from_config(lambda x: x * 2, FAST, seed=1)
        r = q.run(range(50))
        self.assertEqual(len(r.completed), 50)
        self.assertEqual(len(r.staged), 0)

    def test_failures_are_staged_not_dropped(self):
        def work(i):
            if i % 10 == 0:
                raise RuntimeError("boom")
            return i

        q = NoCoordinatorQueue.from_config(work, FAST, seed=1)
        r = q.run(range(50))
        self.assertEqual(len(r.completed) + len(r.staged), 50)
        self.assertEqual(len(r.staged), 5)

    def test_refusals_are_retried_and_are_not_failures(self):
        seen = {}

        def work(i):
            seen[i] = seen.get(i, 0) + 1
            if seen[i] == 1:
                raise Refused()       # refuse once, succeed on the retry
            return i

        q = NoCoordinatorQueue.from_config(work, FAST, seed=1)
        r = q.run(range(20))
        self.assertEqual(len(r.completed), 20)
        self.assertEqual(len(r.staged), 0, "a refusal must never be recorded as a failure")
        self.assertEqual(r.refusals, 20)


class RefusalPredicate(unittest.TestCase):
    """The predicate decides the contract, so it must not be able to break it."""

    def test_throwing_predicate_does_not_lose_the_item(self):
        # Regression: a predicate raising inside the except block killed the worker
        # thread, leaving the item in neither pending, completed nor staged.
        def exploding_predicate(exc):
            raise ValueError("predicate is broken")

        q = NoCoordinatorQueue.from_config(
            lambda i: (_ for _ in ()).throw(RuntimeError("work failed")),
            FAST, is_refusal=exploding_predicate, seed=1)
        r = q.run(range(10))
        self.assertEqual(len(r.completed) + len(r.staged), 10, "an item was lost")
        self.assertEqual(len(r.staged), 10, "undecidable must mean 'not a refusal'")


class Tuning(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            NoCoordinatorQueue.from_config(lambda x: x, {"decrase_factor": 0.5})

    def test_decrease_factor_bounds(self):
        for bad in (0.0, 1.0, 1.5, -0.5):
            with self.subTest(decrease_factor=bad), self.assertRaises(ValueError):
                NoCoordinatorQueue.from_config(lambda x: x, {"decrease_factor": bad})

    def test_retry_window_must_be_ordered(self):
        with self.assertRaises(ValueError):
            NoCoordinatorQueue.from_config(
                lambda x: x, {"retry_floor_ms": 50, "retry_ceiling_ms": 5})

    def test_defaults_are_the_single_source(self):
        q = NoCoordinatorQueue(lambda x: x)
        self.assertEqual(q.decrease_factor, DEFAULT_TUNING["decrease_factor"])
        self.assertEqual(q.backoff_cooldown_ms, DEFAULT_TUNING["backoff_cooldown_ms"])


class ControlLoop(unittest.TestCase):
    def test_limit_finds_a_hidden_ceiling(self):
        capacity = 6
        lock = threading.Lock()
        busy = [0]
        peak = [0]

        def work(i):
            with lock:
                if busy[0] >= capacity:
                    raise Refused()
                busy[0] += 1
                peak[0] = max(peak[0], busy[0])
            try:
                return i
            finally:
                with lock:
                    busy[0] -= 1

        q = NoCoordinatorQueue.from_config(work, FAST, seed=7)
        r = q.run(range(400))
        self.assertEqual(len(r.completed), 400)
        self.assertLessEqual(peak[0], capacity, "never exceeded the ceiling")

    def test_transient_failure_does_not_move_the_limit(self):
        q = NoCoordinatorQueue.from_config(lambda i: 1 / 0, FAST, seed=1)
        before = q.limit
        r = q.run(range(5))
        self.assertEqual(len(r.staged), 5)
        self.assertEqual(q.limit, before, "a real failure is not a capacity signal")

    def test_decrease_reports_movement_only(self):
        q = NoCoordinatorQueue.from_config(lambda x: x, FAST, seed=1)
        q.limit = 1.0                       # already at the floor
        self.assertFalse(q._decrease(), "no movement at the floor must report False")


class Seeding(unittest.TestCase):
    def test_seed_is_stable_across_processes(self):
        # Regression: hash(name) is randomised per process, so a "reproducible" default
        # was not reproducible at all.
        self.assertEqual(_stable_seed("w1"), _stable_seed("w1"))
        self.assertNotEqual(_stable_seed("w1"), _stable_seed("w2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

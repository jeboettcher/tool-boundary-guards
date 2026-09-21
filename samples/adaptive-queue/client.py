"""One consumer process, using NoCoordinatorQueue against the test API.

    python client.py --name w1 --records 12
    python client.py --name w2 --records 12 --url http://127.0.0.1:8099

Run several of these in separate terminals against one `service.py`. They will divide its
lanes between them. They do not know how many lanes exist, and they do not know about each
other. Nothing is configured when one joins or leaves.

The only thing this file contributes to the algorithm is the refusal predicate: HTTP 429
means no capacity, and anything else that goes wrong is a real failure.
"""

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request

from adaptive import NoCoordinatorQueue, Refused


def make_work_fn(url, timeout):
    def do_work(item):
        payload = json.dumps({"a": item["a"], "b": item["b"]}).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # The capacity signal. Nothing was attempted.
                raise Refused("no lane available")
            raise                              # a real failure: staged, not retried
        expected = item["a"] + item["b"]
        if body.get("sum") != expected:
            raise ValueError("wrong answer for %s: %r" % (item["id"], body))
        return {"id": item["id"], "sum": body["sum"], "lane_seconds": body.get("lane_seconds")}
    return do_work


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default="w1")
    p.add_argument("--url", default="http://127.0.0.1:8099/work")
    p.add_argument("--records", type=int, default=12)
    p.add_argument("--config", default=None,
                   help="tuning JSON. The control-loop constants are deployed, not compiled.")
    p.add_argument("--max-limit", type=int, default=None, help="overrides the config")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--json", action="store_true", help="final stats as one JSON line")
    args = p.parse_args()

    tuning = {}
    if args.config:
        with open(args.config) as fh:
            tuning = json.load(fh)

    rng = random.Random(hash(args.name) & 0xFFFF)
    items = [{"id": "%s-%03d" % (args.name, i),
              "a": rng.randint(1, 999), "b": rng.randint(1, 999)}
             for i in range(args.records)]

    started = time.monotonic()

    def on_event(ev):
        if not args.json:
            print("  [%s] %-8s limit=%d  t=%5.1fs"
                  % (ev["name"], ev["event"], ev["limit"], time.monotonic() - started),
                  flush=True)

    overrides = {"on_event": on_event, "name": args.name}
    if args.max_limit is not None:
        overrides["max_limit"] = args.max_limit
    q = NoCoordinatorQueue.from_config(make_work_fn(args.url, args.timeout),
                                       tuning, **overrides)

    if not args.json:
        print("%s: %d records -> %s" % (args.name, args.records, args.url), flush=True)

    result = q.run(items)
    stats = dict(result.stats(), name=args.name)

    if args.json:
        print(json.dumps(stats), flush=True)
    else:
        print("\n%s done: completed=%d staged=%d attempts=%d refusals=%d "
              "final_limit=%d elapsed=%.1fs throughput=%.2f/s"
              % (args.name, stats["completed"], stats["staged"], stats["attempts"],
                 stats["refusals"], stats["final_limit"], stats["elapsed"],
                 stats["throughput"]), flush=True)
        for s in result.staged:
            print("   staged %s: %s" % (s["item"]["id"], s["error"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

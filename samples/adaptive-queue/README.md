<!-- EVIDENCE
AUDIENCE: EXTERNAL
12 | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue/tests/test_adaptive.py | executed via unittest discover, all passing
impossible | PRIMARY: artifacts/public-staging/tool-boundary-guards/samples/adaptive-queue/adaptive.py | _validate rejects decrease_factor outside 0..1 exclusive, an inverted retry window, and negative waits
429 | NOT-A-CLAIM: HTTP status code in an illustrative snippet
300 | NOT-A-CLAIM: a command-line argument value
9 | NOT-A-CLAIM: a command-line argument value
4 | NOT-A-CLAIM: a command-line argument value
6 | NOT-A-CLAIM: a command-line argument value
-->

# NoCoordinatorQueue, Python

Adaptive concurrency against an unknown ceiling shared with instances you cannot see.

Hand it any callable and a way to recognise a refusal. It runs that callable as
concurrently as the far side tolerates, discovers the ceiling from refusals alone, and
divides it with other instances without exchanging a byte with them.

```python
from adaptive import NoCoordinatorQueue, Refused

def charge(order):
    r = requests.post(VENDOR, json=order)
    if r.status_code == 429:
        raise Refused()      # capacity signal: nothing was attempted
    r.raise_for_status()     # anything else is a real failure
    return r.json()

result = NoCoordinatorQueue(charge).run(orders)
result.completed    # return values
result.staged       # failed, parked with the error, never dropped
result.stats()
```

Standard library only.

This is the Python sibling of `../adaptive-queue-csharp/`. Both implement the same
algorithm and both are exercised against the same C# service, so a behavioural divergence
between them is a bug in one of them.

## Run it

`example_inprocess.py` needs nothing but Python. It drives the queue against a plain
in-memory function to show the control loop is not HTTP-specific:

```
python example_inprocess.py --capacity 9 --items 300
```

`client.py` talks to the C# lane service in the sibling directory. Start that first:

```
cd ../adaptive-queue-csharp
dotnet run --project LaneService -- --lanes 4

cd ../adaptive-queue
python client.py --name py1 --records 6
```

Run several clients, in either language, against the one service. They will divide its
lanes without being told how many there are and without knowing about each other.

## Tests

```
python -m unittest discover -s tests -v
```

12 cases. Each exists because the behaviour it checks was once wrong: an item lost when the
refusal predicate itself raised, a refusal recorded as a failure, tuning values accepted
that defeat the control loop, a per-process seed that made "reproducible" runs anything but.

## The refusal predicate is the contract

A refusal means capacity was unavailable and nothing was attempted. The item goes back to
be retried, untouched, counted as a failure nowhere. Anything else means the call happened
and went wrong, so the item is staged with its error.

Getting this boundary wrong is the most common way to break the algorithm. A timeout is
usually not a refusal. A server error is usually not a refusal. Treat ordinary flakiness as
a capacity signal and the fleet throttles itself to the floor and never recovers.

If the predicate itself raises, the item is staged rather than lost. An exception is not a
decision, and "I cannot tell" must never be read as "capacity unavailable".

## Tuning

Every value in the control loop is an operational knob, expressed in milliseconds so the
same file works for the C# client.

```
python client.py --name py1 --config tuning.json
```

`from_config` rejects an unknown key and the constructor rejects an impossible value. Both
halves are needed: a misspelled key and a nonsensical value both leave you believing you
tuned something you did not.

## Files

| file | what it is |
|---|---|
| `adaptive.py` | the queue. The only file you need to use it. |
| `client.py` | one consumer process. Its only contribution is the refusal predicate. |
| `example_inprocess.py` | the same queue against a plain function, no HTTP. |
| `tuning.json` | the control-loop constants, as deployed configuration. |
| `tests/` | regression tests, standard library only. |

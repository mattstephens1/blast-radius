# Blast Radius: test selection for sqlglot

Entry for the RWX *Blast Radius* track at the Builders Buildathon (Nov 2026).

## Problem

For each code change to sqlglot, choose which of its ~1,600 tests to run and
how to split them across up to 8 workers. Cost per commit = busiest worker's
test seconds + 2 s per worker, plus 300 s for every commit where a failing test
was left out. Lower is better.

## Approach

1. **Coverage map (built once, offline).** Run sqlglot's suite at the pinned
   commit under `coverage`, switching context per test, and record which source
   lines each test executes and which fixture files it opens
   (`tools/coverage_plugin.py`, `tools/make_map.py`). The optimizer and executor
   tests use process pools, which coverage can't see into, so during mapping
   the pools are swapped for threads that still pickle arguments and results.
   That way the serialisation code paths get recorded too. The map is stored
   compressed in `maps/` (~0.5 MB).
2. **Per patch (at run time, `app/candidates.py`):**
   - Python files: parse old and new code into syntax trees. If the trees are
     identical (comment/whitespace-only edit), select nothing.
   - Changed functions: select tests that executed any line of them.
   - Import-time changes (class attributes, constants, new methods): select
     tests that executed that class. For a dialect, also select every test
     that touched any of that dialect's modules.
   - Fixture files: the tests that opened them. Test files: all their tests.
   - Anything unrecognised, a syntax error or a crash: run everything (never
     miss).
3. **Split (`app/split.py`):** longest-test-first onto the least-loaded worker,
   trying 1 to 8 workers and keeping the cheapest.

## Result (30 sample commits, local scorer)

| Version | Simulated seconds | Misses |
| --- | --- | --- |
| Starter (every test, 3 workers) | ~1,209 | 0 |
| This selector | 177.2 | 0 |

The selector itself takes about 3 CPU seconds for the whole batch.

## Run

```sh
make sample   # build, run on the sample commits, score
```

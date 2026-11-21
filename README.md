# Blast Radius: test selection for sqlglot

Entry for the RWX *Blast Radius* track at the Builders Buildathon (Nov 2026).

## Problem

For each code change to sqlglot, choose which of its ~1,600 tests to run and
how to split them across up to 8 workers. Cost per commit = busiest worker's
test seconds + 2 s per worker, plus 300 s for every commit where a failing test
was left out. Lower is better.

## Approach

1. **Coverage map (built once, offline).** Run sqlglot's suite at the pinned
   commit under `coverage`, switching context for each test and phase
   (setup / call / teardown), and record which source lines each test
   executes and which fixture files it opens (`tools/coverage_plugin.py`,
   `tools/make_map.py`). Details that matter:
   - The optimizer and executor tests use process pools, which coverage can't
     see into, so during mapping the pools become threads that still pickle
     arguments and results.
   - Lines and files used in `setUpClass` belong to the whole test class, not
     just the test that happened to run first.
   - Lines that only run while sqlglot is being imported are kept separately.
   - For each dialect, the tests that name it (in their own code or in a
     fixture they read) are recorded.
   The map is stored compressed in `maps/` (~0.5 MB).
2. **Per patch (at run time, `app/candidates.py`):**
   - Python files: compare old and new syntax trees, ignoring comments and
     docstrings. No real change → no tests.
   - A changed function → the tests that ran its body.
   - Import-time changes (class attributes, module constants, new methods,
     functions that run during import):
     - in a dialect → every test that touches that dialect, the dialects built
       on it (postgres → redshift) and code elsewhere that names it;
     - in shared code → every test that runs a line naming the changed thing,
       or every test if that can't be pinned down.
   - Fixture files → the tests that read them. Test files → that file's tests,
     plus files importing it if shared code (e.g. `Validator`) changed.
   - Anything unrecognised, a syntax error or a crash → run everything.
3. **Split (`app/split.py`):** longest test first onto the least-loaded
   worker, trying 1 to 8 workers and keeping the cheapest.

The guiding trade-off: a missed failing test costs 300 s, running every test
costs about 37 s. So whenever the selector is unsure, it runs more.

## Results

| Version | Sample (30 commits) | Practice board |
| --- | --- | --- |
| Starter (every test, 3 workers) | ~1,209 s | 1,614.1 s |
| v1: line coverage only | 177.2 s, 0 misses | 764.6 s (2 misses) |
| v2: import-time, setup and dialect rules | 234.8 s, 0 misses | |

Also checked against synthetic commits (random edits to class attributes,
module constants, functions and fixtures, labelled by running the real test
suite): no misses.

## Run

```sh
make sample   # build, run on the sample commits, score
```

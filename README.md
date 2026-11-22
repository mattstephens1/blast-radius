# Blast Radius: test selection for sqlglot

Entry for the RWX *Blast Radius* track at the Builders Buildathon (Nov 2026).

## Problem

For each change to sqlglot, choose which of its ~1,600 tests to run and how to
split them across up to 8 workers. A commit costs its busiest worker's recorded
test seconds plus 2 s per worker, plus 300 s if any test that fails on it was
left out. The program's own CPU time is added to the total.

The guiding rule throughout: **only leave a test out when it provably cannot
fail.** A miss costs 300 s; running every test costs about 37 s. Every shortcut
below is exact for any commit of its kind, never tuned to the practice board,
and was checked against real test runs (see *Validation*).

## How a commit is analysed (`app/candidates.py`)

1. **Coverage map** (built once, offline: `tools/coverage_plugin.py`,
   `tools/make_map.py`, `maps/coverage.pkl.z`). Which source lines each test
   runs, per test and phase; `setUpClass` lines belong to the whole class;
   process pools become threads during mapping so their work is seen; which
   fixture files each test opens.
2. **Python changes in `sqlglot/`.** Old and new syntax trees are compared,
   ignoring comments and docstrings and treating `is` / `==` against `None` or an
   enum member as equal. A changed function selects the tests that ran the
   changed *statement*. Unchanged code selects nothing.
3. **Import-time changes** (class attributes, module constants, functions that
   run while sqlglot is imported, such as the parser building DuckDB's SQL
   templates):
   - **Import-state check** (`app/import_state.py`): the patched package is
     imported and every module global and class attribute is fingerprinted and
     compared with a baseline taken at image build. Nothing changed → only the
     changed lines' tests. Changed dicts and sets report *which items* changed.
     A patched import that fails or hangs → every test.
   - **Read map** (`tools/read_plugin.py`, `maps/reads.pkl.z`): an offline test
     run that records, per test, which items of sqlglot's import-time dicts and
     sets, and which plain class attributes, it reads at run time (the
     generator's dispatch cache is mapped back to `TRANSFORMS`; process pools
     become threads). A changed value selects the tests that read it; e.g. a
     change to one Snowflake tokenizer escape selects 3 tests instead of 346.
   - Values derived elsewhere (`Parser.FUNCTIONS` is built from every
     expression's `_sql_names`) appear in the import-state diff too; expression
     classes are also followed by their SQL names in test code and fixtures.
4. **Test files.** An edit confined to test methods selects only those tests
   (by name, in the file and in files importing it); anything else in a test
   module (helpers, `setUp`, base classes) selects the file and its importers.
5. **Fixtures** select the tests that read them. **Files no test reads** (docs,
   README, CI config) select nothing; packaging files select the packaging tests.
6. **The two slow tests.** `test_max_nodes` (28.8 s) and `test_execute_tpcds`
   (21.5 s) run most of sqlglot, so coverage selects them for most changes.
   Each has an exact check that runs in the commit's patched process:
   - `test_max_nodes` (`app/maxnodes_check.py`): evaluates the test's own
     assertions at reduced size. The node count is exactly linear in the number
     of repeated columns (checked at three widths) and is extrapolated to the full
     size (1,000,001 against a 1,000,000 limit, so it must be exact).
   - TPC executor tests (`app/tpc_check.py`): for each TPC query, fingerprints
     everything the tests prepare (parse, pickle round trip through the tests'
     process pool, DuckDB SQL, optimized query, plan, generated Python code).
     Where it differs, it evaluates the tests' own assertion for that query:
     DuckDB's answer against sqlglot's executor, compared with pandas'
     `assert_frame_equal`. Per-query traces (functions called, values read, for
     preparation and for row evaluation) limit the work to queries the patch can
     reach. DuckDB, pandas and numpy are installed at the test suite's pinned
     versions.
7. **One patched process per commit** (`app/patched_worker.py`) serves all of
   these checks, with CPU budgets, so a pathological patch cannot burn time.

Splitting (`app/split.py`): longest test first onto the least-loaded worker,
trying 1 to 8 workers and keeping the cheapest.

Everything costly is precomputed: the maps are committed; baselines, the source
word index and the list of functions run at import are built during the image
build, which is not charged.

## Running tests inside the container (off by default)

The brief allows running tests while deciding, and a commit whose few tests all
pass needs no worker at all. `app/run_tests.py` and `app/test_server.py` do it
soundly: process pools become threads, tests that spawn Python subprocesses are
never trusted, a test that exceeds 5x its recorded time counts as unknown, and
patched function bodies are swapped into one shared process (closures built at
import included). For the Validator-based dialect tests, `tools/call_plugin.py`
records which sqlglot functions each assertion call executes, so the shared
process can skip assertion calls that cannot see a body-only change.

All of it was validated (every test it left out passed under pytest), but on the
practice board the scorer runs tests at roughly 1.5-2x their recorded time, and
no setting paid for its CPU (table below). `RUN_TESTS=1` turns it on.

## Validation

- The sample's 30 commits, against its answers.
- 400 synthetic one-token mutations (`tools/dev/gen.py`), labelled by running
  sqlglot's real test suite in a separate image (`tools/dev/labeler`): every
  narrowing rule's selections were checked against full-suite labels, and every
  test a check or the runner leaves out was re-run under pytest. This caught,
  among others, a pickling failure that crashes the TPC tests' process pool,
  sqlglot's test-only validation (`UNITTEST`) that the checks must reproduce,
  process-pool work invisible to the read map, and closures a factory builds at
  import keeping old code.
- Practice-board diagnostic builds (never entries) measured where the board's
  cost sits: e.g. "never select the two slow tests" (real cost and number of
  commits that really fail them) and "select nothing below 10 s" (how many cheap
  commits fail nothing).

## Results

Sample: **109.624 s simulated, 0 misses** (the floor, running only the failing
tests, is 87.1 s).

| Version | Practice board (simulated + CPU) |
| --- | --- |
| Starter (every test, 3 workers) | 1,614.1 |
| v1: line coverage only | 764.6 (2 misses) |
| v3: narrower name rules | 265.8 |
| v8: dialect class settings narrowed | 226.5 |
| v9: import-state check | 208.4 (202.0 + 6.4) |
| v13: read map for import-time changes | 169.5 (161.5 + 8.0) |
| v16: test-file edits select only changed tests; slow-test checks on mixed commits | 155.7 (141.5 + 14.3) |
| v18: TPC check narrowed per query | 151.4 (141.5 + 9.9) |
| **v19: one patched process per commit, CPU budgets** | **150.2 (141.5 + 8.7)** |
| v21: in-container runner, up to 1.5 s | 154.4 (134.1 + 20.3) |
| v22: shared test server, up to 0.6 s | 151.0 (136.9 + 14.0) |
| v23: runner off | 150.5 (141.5 + 9.0) |
| v24: call-level runner | 153.4 (141.4 + 11.9) |
| v25: runner off (current) | ≈150 |

No version since v2 missed a commit on the practice board.

## Run

```sh
make sample   # build, run on the sample commits, score
```

Rebuilding the offline maps (in an image with sqlglot's test dependencies,
`tools/dev/labeler`): `tools/coverage_plugin.py` + `tools/make_map.py`,
`tools/read_plugin.py` + `tools/make_reads.py`, `tools/call_plugin.py` +
`tools/make_calls.py`, and `app/tpc_check.py --runtime / --query-traces /
--rows-traces`.

"""Can a patch change the outcome of the TPC executor tests?

tests/test_executor.py::test_execute_tpcds (21.5 s) and test_execute_tpch run
each TPC query twice: DuckDB runs sqlglot's DuckDB transpilation, and sqlglot's
own executor runs the parsed query (optimize -> plan -> generate Python code
per step -> evaluate that code row by row). The results must match.

Nearly every sqlglot change is covered by these tests, but few change what they
compute. Everything except the row-by-row evaluation is cheap, so this module
redoes the cheap part with the patched source and fingerprints it per query:
the parse, the pickled parse (it crosses a process pool), the DuckDB SQL, the
tables found, the optimized query, the plan, every piece of Python code the
executor generates, and the result columns. The executor is run on empty
tables, so no rows are evaluated.

If every fingerprint matches the unpatched one (taken at image build time),
and the patch changes no code that the row-by-row evaluation calls (also
recorded at build time, see --baseline), the tests compute exactly what they
did before, so they pass.

    python tpc_check.py --baseline <repo> <fixtures> <out.json>   (image build)
    python tpc_check.py --runtime <repo> <fixtures> <out.json>    (once, offline: maps/tpc_runtime.json)
    python tpc_check.py --check <repo> <fixtures> <baseline.json>  (stdin: {path: source})
"""
# sqlglot turns on extra validation when it is imported under a test runner
# (expressions/core.py: UNITTEST = "unittest" in sys.modules or "pytest" in
# sys.modules). Import unittest first so every check sees what the tests see.
import unittest  # noqa: F401
import hashlib
import json
import os
import pickle
import sys
import time

SUITES = {"tpcds": "tpc-ds/tpc-ds.sql", "tpch": "tpc-h/tpc-h.sql"}
EXECUTE_BUDGET_S = 6.0  # CPU for running changed queries on real data before giving up


def _queries(fixtures, suite):
    from tests import helpers
    helpers.FIXTURES_DIR = os.path.abspath(fixtures)  # the tests/fixtures directory
    return list(helpers.load_sql_fixture_pairs("optimizer/" + SUITES[suite])), helpers


def _setup(repo, overrides):
    sys.path.insert(0, repo)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    if overrides:
        import import_state
        sys.meta_path.insert(0, import_state._Patched(repo, overrides))


def plan_canon(step, memo=None):
    """A plan as text, independent of object ids and set order: each step's
    fields, with its dependencies sorted (back-references to dependents left out)."""
    memo = {} if memo is None else memo
    if id(step) in memo:
        return memo[id(step)]

    def canon(v):
        if hasattr(v, "dependencies") and hasattr(v, "dependents"):
            return plan_canon(v, memo)
        if isinstance(v, dict):
            return "{%s}" % ",".join(sorted("%s:%s" % (canon(k), canon(x)) for k, x in v.items()))
        if isinstance(v, (set, frozenset)):
            return "{%s}" % ",".join(sorted(canon(x) for x in v))
        if isinstance(v, (list, tuple)):
            return "[%s]" % ",".join(canon(x) for x in v)
        return repr(v)

    fields = sorted((k, canon(v)) for k, v in vars(step).items() if k not in ("dependents",))
    memo[id(step)] = out = "%s(%s)" % (type(step).__name__, ",".join("%s=%s" % kv for kv in fields))
    return out


class _Recorder:
    """Wrap the executor's hooks to record the plan and generated code."""

    def __init__(self):
        import sqlglot.executor as ex
        from sqlglot.executor.python import PythonExecutor
        self.log = []
        rec = self
        orig_plan = ex.Plan

        class Plan(orig_plan):
            def __init__(self, expression):
                rec.log.append("optimized " + repr(expression))
                super().__init__(expression)
                rec.log.append("plan " + plan_canon(self.root))

        ex.Plan = Plan
        orig_generate = PythonExecutor.generate

        def generate(self, expression):
            if expression is not None:
                rec.log.append("code " + self.generator.generate(expression))
            return orig_generate(self, expression)

        PythonExecutor.generate = generate


def fingerprints(fixtures, suites, tables=None, stop=None, only=None, duck=None, parsed=None):
    """query key -> fingerprint. stop(key, fingerprint) -> True abandons the rest
    of that suite (one difference is enough to know the suite changed).
    only: the query keys to compute (default: all).
    duck, if given, is filled with query key -> hash of its DuckDB SQL (what the
    DuckDB side of the tests runs); parsed with query key -> hash of the parsed
    query and its pickle round trip (what every query goes through on its way to
    the tests' process pool, executed or not)."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    rec = _Recorder()
    out = {}
    for suite in suites:
        queries, helpers = _queries(fixtures, suite)
        schema = helpers.TPCDS_SCHEMA if suite == "tpcds" else helpers.TPCH_SCHEMA
        for i, (meta, sql, expected) in enumerate(queries):
            if only is not None and "%s %d" % (suite, i) not in only:
                continue
            rec.log = []
            parts = []
            try:
                e = parse_one(sql)
                duck_sql = [transpile(sql, write="duckdb")[0]]
                if suite == "tpch":  # test_optimized_tpch runs the fixture's optimized SQL too
                    duck_sql.append(transpile(expected, write="duckdb")[0])
                if duck is not None:
                    duck["%s %d" % (suite, i)] = hashlib.blake2b("\n".join(duck_sql).encode(),
                                                                digest_size=12).hexdigest()
                key_ = "%s %d" % (suite, i)
                if parsed is not None:
                    parsed[key_] = "error"  # until the round trip below succeeds
                trip = [repr(e), repr(pickle.loads(pickle.dumps(e)))]
                if parsed is not None:
                    parsed[key_] = hashlib.blake2b("\n".join(trip).encode(), digest_size=12).hexdigest()
                parts += trip + duck_sql
                if meta.get("execute"):
                    names = [t.name for t in find_tables(e)]
                    parts.append(repr(names))
                    data = {n: (tables[suite][n] if tables else Table(columns=list(schema[n]), rows=[]))
                            for n in names}
                    result = execute(e, schema=schema, tables=data)
                    parts.append(repr(result.columns))
            except Exception as error:
                parts.append("error " + type(error).__name__ + " " + str(error)[:200])
                if duck is not None:
                    duck.setdefault("%s %d" % (suite, i), "error")
            parts += sorted(rec.log)  # independent steps run in id() order
            if os.environ.get("TPC_DUMP"):
                with open(os.environ["TPC_DUMP"] + ("full" if tables else "empty"), "a") as f:
                    f.write("=== %s %d\n%s\n" % (suite, i, "\n".join(parts)))
            key = "%s %d" % (suite, i)
            out[key] = hashlib.blake2b("\n".join(parts).encode(), digest_size=12).hexdigest()
            if stop is not None and stop(key, out[key]):
                break
    return out


def results(fixtures, suite, keys, cache):
    """query key -> hash of what sqlglot's executor returns for it on the real
    data (columns and rows: the executor side of the tests' comparison).

    The tests run each query in a process pool: the tables go to the workers
    pickled (initializer args), the parsed query goes pickled, and the result
    table comes back pickled. All three round trips are reproduced here."""
    from sqlglot import find_tables, parse_one
    from sqlglot.executor import execute
    queries, helpers = _queries(fixtures, suite)
    schema = helpers.TPCDS_SCHEMA if suite == "tpcds" else helpers.TPCH_SCHEMA
    out = {}
    for key in keys:
        i = int(key.split()[1])
        meta, sql, _ = queries[i]
        try:
            e = parse_one(sql)
            names = [t.name for t in find_tables(e)]
            for n in names:
                if (suite, n) not in cache:
                    loaded = _load_tables(fixtures, suite, [n])[n]
                    cache[(suite, n)] = pickle.loads(pickle.dumps(loaded))  # to the worker
            table = execute(pickle.loads(pickle.dumps(e)), schema=pickle.loads(pickle.dumps(schema)),
                            tables={n: cache[(suite, n)] for n in names})
            table = pickle.loads(pickle.dumps(table))  # back from the worker
            text = repr(table.columns) + "\n" + repr(table.rows)
        except Exception as error:
            text = "error " + type(error).__name__ + " " + str(error)[:200]
        out[key] = hashlib.blake2b(text.encode(), digest_size=16).hexdigest()
    return out


def _load_tables(fixtures, suite, names=None):
    """The test's own table loading (setUpClass), for the build-time trace."""
    import ast
    import csv
    import gzip
    from sqlglot.executor.table import Table
    queries, helpers = _queries(fixtures, suite)
    schema = helpers.TPCDS_SCHEMA if suite == "tpcds" else helpers.TPCH_SCHEMA
    d = os.path.join(fixtures, "optimizer", SUITES[suite].split("/")[0])
    tables = {}
    for table, columns in schema.items():
        if names is not None and table not in names:
            continue
        reader = csv.reader(gzip.open(os.path.join(d, table + ".csv.gz"), "rt", newline=""), delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = Table(columns=columns, rows=rows)
    return tables


def runtime_functions(repo, fixtures, suites):
    """sqlglot functions called while rows are evaluated (inside the executor,
    outside code generation), over the full data. Also checks that running on
    full data generates exactly the code the empty-table run does."""
    import sqlglot.executor.python as py
    called = set()
    depth = [0]
    orig_generate = py.PythonExecutor.generate

    def generate(self, expression):
        depth[0] += 1
        try:
            return orig_generate(self, expression)
        finally:
            depth[0] -= 1

    py.PythonExecutor.generate = generate
    orig_execute = py.PythonExecutor.execute
    active = [0]

    def prof(frame, event, arg):
        if event == "call" and active[0] and not depth[0]:
            f = frame.f_code.co_filename
            if f.startswith(repo):
                called.add("%s:%s" % (os.path.relpath(f, repo), frame.f_code.co_qualname))

    def execute(self, plan, outer_scope=None):
        active[0] += 1
        try:
            return orig_execute(self, plan, outer_scope)
        finally:
            active[0] -= 1

    py.PythonExecutor.execute = execute
    tables = {s: _load_tables(fixtures, s) for s in suites}
    # with tools/read_plugin.py available, also record which import-time values
    # (container items, plain class attributes) rows read, so import-time changes
    # can be checked too
    rp = None
    if os.environ.get("READ_PLUGIN_DIR"):
        sys.path.insert(0, os.environ["READ_PLUGIN_DIR"])
        import read_plugin as rp
        rp._install(repo)
        inner_generate = py.PythonExecutor.generate

        def generate_quiet(self, expression):  # code generation is fingerprinted instead
            saved, rp._ctx[0] = rp._ctx[0], None
            try:
                return inner_generate(self, expression)
            finally:
                rp._ctx[0] = saved

        py.PythonExecutor.generate = generate_quiet
        inner_execute = py.PythonExecutor.execute

        def execute_rows(self, plan, outer_scope=None):
            saved, rp._ctx[0] = rp._ctx[0], "rows"
            try:
                return inner_execute(self, plan, outer_scope)
            finally:
                rp._ctx[0] = saved

        py.PythonExecutor.execute = execute_rows
    sys.setprofile(prof)
    try:
        full = fingerprints(fixtures, suites, tables)
    finally:
        sys.setprofile(None)
    py.PythonExecutor.generate = orig_generate
    py.PythonExecutor.execute = orig_execute
    reads = None
    if rp is not None:
        reads = sorted({(k, rp._item_key(i)) for label, i in rp._reads.get("rows", ())
                        for k in rp._labels[label]})
    return called, full, reads


def query_traces(repo, fixtures, suites):
    """For each query, in a fresh process (so lazily built caches are built by the
    query that needs them): the sqlglot functions its preparation calls and the
    import-time values it reads. A query that calls no changed function and reads
    no changed value prepares exactly what it did before."""
    import pickle
    sys.path.insert(0, os.environ["READ_PLUGIN_DIR"])
    import read_plugin as rp
    rp._install(repo)
    keys = []
    for suite in suites:
        queries, _ = _queries(fixtures, suite)
        keys += [(suite, "%s %d" % (suite, i)) for i in range(len(queries))]
    out = {}
    for suite, key in keys:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            called = set()

            def prof(frame, event, arg):
                if event == "call":
                    f = frame.f_code.co_filename
                    if f.startswith(repo):
                        called.add("%s:%s" % (os.path.relpath(f, repo),
                                              frame.f_code.co_qualname.replace(".<locals>", "")))
            rp._ctx[0] = "q"
            sys.setprofile(prof)
            try:
                fingerprints(fixtures, [suite], only={key})
            finally:
                sys.setprofile(None)
            reads = {(k, rp._item_key(i)) for label, i in rp._reads.get("q", ()) for k in rp._labels[label]}
            with os.fdopen(w, "wb") as f:
                pickle.dump((sorted(called), sorted(reads)), f)
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            out[key] = pickle.load(f)
        os.waitpid(pid, 0)
    return out


def main():
    mode, repo, fixtures, path = sys.argv[1:5]
    repo = os.path.abspath(repo)
    suites = sys.argv[5].split(",") if len(sys.argv) > 5 else list(SUITES)
    if mode == "--baseline":  # image build (free): also every executed query's result
        _setup(repo, None)
        duck, parsed = {}, {}
        fp = fingerprints(fixtures, suites, duck=duck, parsed=parsed)
        res, cache = {}, {}
        for suite in suites:
            queries, _ = _queries(fixtures, suite)
            keys = ["%s %d" % (suite, i) for i, (meta, _, _) in enumerate(queries) if meta.get("execute")]
            res.update(results(fixtures, suite, keys, cache))
        with open(path, "w") as f:
            json.dump({"fingerprints": fp, "duck": duck, "parsed": parsed, "results": res}, f)
        return
    if mode == "--runtime":  # minutes: run once offline, output committed to maps/
        _setup(repo, None)
        empty = fingerprints(fixtures, suites)
        t0 = time.time()
        called, full, reads = runtime_functions(repo, fixtures, suites)
        # queries whose generated code depends on the data cannot be checked on empty tables
        data_dependent = sorted(k for k in empty if empty[k] != full.get(k))
        if data_dependent:
            raise SystemExit("generated code depends on the data for %s" % data_dependent)
        with open(path, "w") as f:
            json.dump({"functions": sorted(called), "reads": reads}, f, indent=0)
        print("baseline: %d queries, %d runtime functions, %d data dependent, trace %.0fs"
              % (len(empty), len(called), len(data_dependent), time.time() - t0), file=sys.stderr)
        return
    if mode == "--query-traces":  # offline, with READ_PLUGIN_DIR: maps/tpc_queries.pkl.z
        import pickle
        import zlib
        traces = query_traces(repo, fixtures, suites)
        with open(path, "wb") as f:
            f.write(zlib.compress(pickle.dumps(traces, protocol=5), 9))
        print("%d query traces" % len(traces), file=sys.stderr)
        return
    request = json.load(sys.stdin)
    if "overrides" in request:  # {"overrides": ..., "only": [query keys]}
        overrides, only = request["overrides"], set(request.get("only") or ())
    else:
        overrides, only = request, None
    with open(path) as f:
        base = json.load(f)
    try:
        _setup(repo, overrides)
        fp, base_duck, base_res = base["fingerprints"], base.get("duck", {}), base.get("results", {})
        base_parsed = base.get("parsed", {})
        duck, parsed, cache, really, executed = {}, {}, {}, set(), [0, 0.0]

        def stop(key, value):
            """A differing preparation only matters if the DuckDB SQL differs or the
            executor's result on the real data does: run just this query to see."""
            if fp.get(key) == value:
                return False
            if base_duck.get(key) is None or duck.get(key) != base_duck.get(key):
                really.add(key)
                return True
            if base_parsed.get(key) is None or parsed.get(key) != base_parsed.get(key):
                really.add(key)  # every query is pickled to the process pool
                return True
            if key in base_res:
                t = time.process_time()
                same = results(fixtures, key.split()[0], [key], cache)[key] == base_res[key]
                executed[0] += 1
                executed[1] += time.process_time() - t
                if not same or executed[1] > EXECUTE_BUDGET_S:
                    really.add(key)
                    return True
            return False

        now = fingerprints(fixtures, suites, stop=stop, only=only, duck=duck, parsed=parsed)
    except BaseException as error:
        print(json.dumps({"ok": False, "error": repr(error)[:300]}))
        return
    # an abandoned suite counts as changed through the query that decided it
    changed = sorted(really)
    wanted = [k for k in fp if k.split()[0] in suites and (only is None or k in only)]
    missing = sorted({k.split()[0] for k in wanted if k not in now} - {k.split()[0] for k in changed})
    print(json.dumps({"ok": not missing, "changed": changed, "executed": executed[0],
                      "error": "incomplete %s" % missing}))


if __name__ == "__main__":
    main()

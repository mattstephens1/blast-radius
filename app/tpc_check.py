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


def fingerprints(fixtures, suites, tables=None, stop=None):
    """query key -> fingerprint. stop(key, fingerprint) -> True abandons the rest
    of that suite (one difference is enough to know the suite changed)."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    rec = _Recorder()
    out = {}
    for suite in suites:
        queries, helpers = _queries(fixtures, suite)
        schema = helpers.TPCDS_SCHEMA if suite == "tpcds" else helpers.TPCH_SCHEMA
        for i, (meta, sql, expected) in enumerate(queries):
            rec.log = []
            parts = []
            try:
                e = parse_one(sql)
                parts += [repr(e), repr(pickle.loads(pickle.dumps(e))), transpile(sql, write="duckdb")[0]]
                if suite == "tpch":  # test_optimized_tpch runs the fixture's optimized SQL too
                    parts.append(transpile(expected, write="duckdb")[0])
                if meta.get("execute"):
                    names = [t.name for t in find_tables(e)]
                    parts.append(repr(names))
                    data = {n: (tables[suite][n] if tables else Table(columns=list(schema[n]), rows=[]))
                            for n in names}
                    result = execute(e, schema=schema, tables=data)
                    parts.append(repr(result.columns))
            except Exception as error:
                parts.append("error " + type(error).__name__ + " " + str(error)[:200])
            parts += sorted(rec.log)  # independent steps run in id() order
            if os.environ.get("TPC_DUMP"):
                with open(os.environ["TPC_DUMP"] + ("full" if tables else "empty"), "a") as f:
                    f.write("=== %s %d\n%s\n" % (suite, i, "\n".join(parts)))
            key = "%s %d" % (suite, i)
            out[key] = hashlib.blake2b("\n".join(parts).encode(), digest_size=12).hexdigest()
            if stop is not None and stop(key, out[key]):
                break
    return out


def _load_tables(fixtures, suite):
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


def main():
    mode, repo, fixtures, path = sys.argv[1:5]
    repo = os.path.abspath(repo)
    suites = sys.argv[5].split(",") if len(sys.argv) > 5 else list(SUITES)
    if mode == "--baseline":  # cheap: run at image build
        _setup(repo, None)
        with open(path, "w") as f:
            json.dump({"fingerprints": fingerprints(fixtures, suites)}, f)
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
    overrides = json.load(sys.stdin)
    with open(path) as f:
        base = json.load(f)
    try:
        _setup(repo, overrides)
        fp = base["fingerprints"]
        now = fingerprints(fixtures, suites, stop=lambda k, v: fp.get(k) != v)
    except BaseException as error:
        print(json.dumps({"ok": False, "error": repr(error)[:300]}))
        return
    # an abandoned suite counts as changed through its first differing query
    changed = sorted(k for k in now if fp.get(k) != now[k])
    missing = sorted({k.split()[0] for k in fp if k.split()[0] in suites and k not in now}
                     - {k.split()[0] for k in changed})
    print(json.dumps({"ok": not missing, "changed": changed, "error": "incomplete %s" % missing}))


if __name__ == "__main__":
    main()

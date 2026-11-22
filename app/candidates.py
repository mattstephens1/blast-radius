"""Turn a patch into the set of tests that could notice it.

The rule throughout: when unsure, select more. A missed failing test costs
300 s; running every test costs about 37 s.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import analyse
import patchlib

IMPORT_STATE = Path(__file__).resolve().with_name("import_state.py")
INDEX_FILE = Path(os.environ.get(
    "INDEX_FILE", Path(__file__).resolve().parent.parent / "maps" / "index.marshal"))
TPC_CHECK = Path(__file__).resolve().with_name("tpc_check.py")
TPC_BASELINE = Path(os.environ.get(
    "TPC_BASELINE", Path(__file__).resolve().parent.parent / "maps" / "tpc_state.json"))
TPC_RUNTIME = Path(__file__).resolve().parent.parent / "maps" / "tpc_runtime.json"
TPC_FIXTURES = os.environ.get("TPC_FIXTURES")  # default: <repo>/tests/fixtures
# the TPC executor tests and the suite whose fingerprints each depends on;
# "runtime" ones also evaluate rows with sqlglot's executor
TPC_TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("tpcds", True),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("tpch", True),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("tpch", False),
}
# narrowing import-time value changes by the changed names: small gain, not yet
# verified against real test runs, so off
NAME_NARROW = False
RUN_TESTS = Path(__file__).resolve().with_name("run_tests.py")
# Run small selections here (app/run_tests.py, the shared test server or the commit's
# worker) and leave out the tests that pass. Sound (161 + 232 dropped tests confirmed
# by pytest), but on the practice board the scorer runs tests at ~1.5-2x their
# recorded time, so it cost about what it saved (v21: -7.4 s simulated, +11.6 s CPU;
# v22: -4.5 / +5.3). Off by default; RUN_TESTS=1 turns it on.
RUN_ENABLED = os.environ.get("RUN_TESTS", "1") == "1"
RUN_MAX_S = float(os.environ.get("RUN_MAX_S", "1.5"))  # run selections taking at most this long...
RUN_WORTH_S = 2.0   # ...and leaving them all out would save at least this much
RUN_TIMEOUT_S = 30  # on top of 5x the tests' recorded time
# test files whose import or class setup costs more than a cleared commit saves
# (test_optimizer imports pandas and DuckDB; TestExecutor loads every TPC table)
RUN_SKIP_FILES = tuple(f for f in os.environ.get(
    "RUN_SKIP_FILES", "tests/test_optimizer.py,tests/test_executor.py").split(",") if f)
# without call skipping (patches that change more than function bodies) a run costs
# the tests' full time: only tiny selections then
RUN_MAX_FULL_S = float(os.environ.get("RUN_MAX_FULL_S", "0.3"))
TPC_WORTH_S = 8.0
MAXNODES_CHECK = Path(__file__).resolve().with_name("maxnodes_check.py")
MAXNODES_BASELINE = Path(os.environ.get(
    "MAXNODES_BASELINE", Path(__file__).resolve().parent.parent / "maps" / "maxnodes.json"))
MAXNODES_TEST = "tests/test_parser.py::TestParser::test_max_nodes"
MAXNODES_WORTH_S = 6.0  # simulated seconds leaving it out must save (the check costs ~1-2 s CPU)
READS = Path(__file__).resolve().parent.parent / "maps" / "reads.pkl.z"
DIFF_ITEMS = {}  # changed dict/set attribute -> its changed items, from the last import check
# containers that sqlglot reads at run time through C fast paths the read map
# cannot see (dialect.py: kwargs.keys() - self.SUPPORTED_SETTINGS); they are
# followed by name as well. Found with tools/dev/c_reads.sh.
READ_BY_C = {"SUPPORTED_SETTINGS"}
# On by default since it also covers import-time changes (row-phase reads): the
# board showed test_execute_tpcds / test_max_nodes costing ~57 s while failing in
# one commit. TPC_CHECK=0 turns it off.
TPC_ENABLED = os.environ.get("TPC_CHECK", "1") == "1"
IMPORT_BASELINE = Path(os.environ.get(
    "IMPORT_BASELINE", Path(__file__).resolve().parent.parent / "maps" / "import_state.json"))

CHECK_WORTH_S = 3.0  # test seconds an import-state check must be able to save

# files outside sqlglot/ and tests/ that a test reads, and the tests that read them
PACKAGING = ("setup.py", "setup.cfg", "pyproject.toml", "MANIFEST.in", "sqlglotc", "requirements.txt")
PACKAGING_TESTS = ("tests/test_sqlglotc.py", "tests/test_build.py")

FAMILY_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")
BIG = 10**7


class Maps:
    def __init__(self, lines, import_lines, opened, unattributed_opens, tests, repo, mentions=None):
        self.lines = lines                       # path -> line -> frozenset of test numbers
        self.import_lines = {f: set(v) for f, v in import_lines.items()}
        self.opened = opened                     # fixture path -> set of test numbers
        self.unattributed = set(unattributed_opens)  # files opened while no test was running
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self.all = {t["number"] for t in tests}
        self.tests = tests
        self.secs = {t["number"]: t["seconds"] for t in tests}
        self.repo = repo
        self.mentions = mentions or {}
        self._src = {}

    def seconds(self, nums):
        return sum(self.secs.get(n, 0) for n in nums)

    def covering(self, path, lo=1, hi=BIG):
        out = set()
        for ln, nums in self.lines.get(path, {}).items():
            if lo <= ln <= hi:
                out |= nums
        return out

    def runs_at_import(self, path, lo, hi):
        return any(lo <= ln <= hi for ln in self.import_lines.get(path, ()))

    def index(self):
        """word -> [(path, line)], exact case and lower case: loaded from the file
        written at image build time (write_index), else built here."""
        if not hasattr(self, "_idx"):
            if INDEX_FILE.exists():
                import marshal
                self._idx, self._lidx = marshal.loads(INDEX_FILE.read_bytes())
            else:
                self._idx, self._lidx = build_index(self.source_lines())
        return self._idx, self._lidx

    def source_lines(self):
        if not self._src:
            for p in self.repo.glob("sqlglot/**/*.py"):
                self._src[str(p.relative_to(self.repo))] = p.read_text().split("\n")
        return self._src

    def files_naming(self, name):
        """Files that read this name (lines that only assign it, NAME = ..., don't count)."""
        assign = re.compile(r"^\s*%s\s*[:=]" % re.escape(name))
        src = self.source_lines()
        return {p for p, i in self.index()[0].get(name, ()) if not assign.match(src[p][i - 1])}

    def is_specific(self, name, home):
        """A name used in only a few modules (WEEK_OFFSET), not everywhere (arg_types, KEYWORDS)."""
        return len(self.files_naming(name) - {home}) <= 3

    def referencing(self, names):
        """Tests that ran a sqlglot line mentioning one of these identifiers.

        Only lines that run during tests count. A mention that only runs at
        import time (an entry in a class-level table) matters only when the
        thing it names turns up at run time, which the runtime mentions cover.
        """
        out = set()
        exact = self.index()[0]
        for name in names:
            for path, i in exact.get(name, ()):
                out |= self.lines.get(path, {}).get(i, set())
        return out


def build_index(source_lines):
    exact, lower = {}, {}
    word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    for path, lines in source_lines.items():
        for i, text in enumerate(lines, 1):
            for w in set(word.findall(text)):
                exact.setdefault(w, []).append((path, i))
                lower.setdefault(w.lower(), []).append((path, i))
    return exact, lower


def write_index(repo, dest):
    """Image build step: the source index, so runs load it instead of building it."""
    import marshal
    m = Maps({}, {}, {}, [], [], Path(repo))
    Path(dest).write_bytes(marshal.dumps(build_index(m.source_lines())))


def _dialect_of(path):
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in FAMILY_DIRS \
            and parts[2].endswith(".py") and parts[2] != "__init__.py":
        return parts[2][:-3]
    return None


_SUBCLASS_CACHE = {}


def _subclass_dialects(name, maps):
    """Dialects whose modules import this dialect's modules (e.g. redshift <- postgres)."""
    if name in _SUBCLASS_CACHE:
        return set(_SUBCLASS_CACHE[name])
    _SUBCLASS_CACHE[name] = out = _find_subclass_dialects(name, maps)
    return set(out)


def _find_subclass_dialects(name, maps):
    found, todo = {name}, [name]
    src, exact = maps.source_lines(), maps.index()[0]
    while todo:
        cur = todo.pop()
        pat = re.compile(r"sqlglot\.(?:%s)\.%s\b" % ("|".join(FAMILY_DIRS), re.escape(cur)))
        for path, i in exact.get(cur, ()):
            other = _dialect_of(path)
            if other and other not in found and pat.search(src[path][i - 1]):
                found.add(other)
                todo.append(other)
    return found


_FAMILY_CACHE = {}
_FAM_COV_CACHE = {}  # name -> covering-only subset of family (no mentions/borrowers)


def family(path, maps):
    """Every test that touches a dialect: ran any of its modules, or names it.

    Includes dialects built on top of it (a change to postgres reaches redshift).
    Returns None for files that are not dialect-specific.
    """
    name = _dialect_of(path)
    if name is None or name == "dialect":
        return None
    if name in _FAMILY_CACHE:
        return set(_FAMILY_CACHE[name])
    out = set()
    cov = set()
    for dialect in _subclass_dialects(name, maps):
        for d in FAMILY_DIRS:
            c = maps.covering(f"sqlglot/{d}/{dialect}.py")
            out |= c
            cov |= c
        out |= set(maps.mentions.get(dialect, ()))
        out |= _borrowers(dialect, maps)
    _FAMILY_CACHE[name] = out
    _FAM_COV_CACHE[name] = cov
    return set(out)


def family_covering(path, maps):
    """Tests that ran any line in this dialect family's source files.

    Subset of family(): excludes mentions and borrowers. Safe for pure class
    attribute changes where only dialect-specific runtime code reads the attr.
    Returns None for non-dialect files.
    """
    name = _dialect_of(path)
    if name is None or name == "dialect":
        return None
    if name not in _FAM_COV_CACHE:
        family(path, maps)  # populate cache
    return set(_FAM_COV_CACHE.get(name, ()))


def _borrowers(dialect, maps):
    """Tests that run code elsewhere naming this dialect, e.g. presto parsing
    TO_CHAR with build_formatted_time(..., "teradata")."""
    out, import_files = set(), set()
    for path, i in maps.index()[1].get(dialect.lower(), ()):
        if _dialect_of(path) == dialect:
            continue
        cov = maps.lines.get(path, {})
        if i in cov:
            out |= cov[i]
        elif _dialect_of(path) not in (None, "dialect"):
            import_files.add(path)
    for path in import_files:
        out |= maps.covering(path)
    return out


WORKER = Path(__file__).resolve().with_name("patched_worker.py")


class PatchedWorker:
    """The commit's patched sqlglot, imported once in one process
    (app/patched_worker.py), answering every check that needs it."""

    def __init__(self, repo, overrides):
        self.repo, self.overrides, self.proc, self.dead = repo, overrides, None, False

    def call(self, request, timeout, notes, label):
        if self.dead:
            return None
        import select
        try:
            if self.proc is None:
                env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
                self.proc = subprocess.Popen([sys.executable, str(WORKER), str(self.repo)],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, env=env)
                self.proc.stdin.write(json.dumps({"overrides": self.overrides}) + "\n")
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
            ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
            line = self.proc.stdout.readline() if ready else ""
            if not line:
                raise TimeoutError("%s: no answer in %ss" % (label, timeout))
            return json.loads(line)
        except Exception as error:
            notes.append((label + " failed", repr(error)[:100]))
            self.close()
            self.dead = True
            return None

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None


CURRENT = [None]  # the PatchedWorker of the commit being analysed


def import_state_diff(repo, overrides, notes):
    """Module globals and class attributes ("module:Class.ATTR") whose value after
    importing sqlglot from the patched source differs from the unpatched one
    (see import_state.py). [] means nothing differs; None means unknown, or the
    patched package does not import at all."""
    if not IMPORT_BASELINE.exists():
        return None
    worker = CURRENT[0]
    if worker is None:
        return None
    result = worker.call({"op": "import_diff", "baseline": str(IMPORT_BASELINE)}, 5, notes,
                         "import state check")
    if result is None:
        return None
    if not result.get("ok"):
        notes.append(("patched import fails", result.get("error", "")[:100]))
        return None
    if result["changed"]:
        notes.append(("import state changed", result["changed"][:5]))
    DIFF_ITEMS.clear()
    DIFF_ITEMS.update(result.get("items") or {})
    return result["changed"]


def _run_json(args, overrides, notes, label, timeout):
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    try:
        run = subprocess.run([sys.executable] + [str(a) for a in args], input=json.dumps(overrides),
                             capture_output=True, text=True, env=env, timeout=timeout)
        return json.loads(run.stdout.strip().splitlines()[-1])
    except Exception as error:
        notes.append((label + " failed", repr(error)[:100]))
        return None


def rows_read_nothing_changed(import_diff, row_reads, row_keys, d, maps):
    """True when no import-time value the patch changes is read while the TPC
    executor tests evaluate rows (traced offline, see tpc_check.py --runtime)."""
    if row_reads is None:
        return False
    changed = import_diff()
    if changed is None:
        return False
    reads = _reads(maps)
    tracked = reads["keys"] if reads else {}
    funcs = {q.rsplit(".", 1)[-1] for q, _ in d.functions}
    for key in changed:
        name = key.split(":", 1)[1].rsplit(".", 1)[-1]
        if key not in row_keys:
            if key in tracked or name in funcs:
                continue  # traced and never read by rows / a changed function (checked by name)
            return False  # not traced (a module constant, a class...): assume rows read it
        items = DIFF_ITEMS.get(key)
        if items is None or (key, "<all>") in row_reads or any((key, i) in row_reads for i in items):
            return False
    return True


TPC_QUERIES = Path(__file__).resolve().parent.parent / "maps" / "tpc_queries.pkl.z"


def import_time_change(files, overrides, repo, maps):
    """Does any of these patched sqlglot files change something that runs at import?"""
    for f in files:
        path = f["new"]
        old = (repo / path).read_text() if (repo / path).exists() else ""
        d = analyse.diff_python(old, overrides[path], patchlib.changed_old_lines(f["hunks"]),
                                patchlib.changed_new_lines(f["hunks"]))
        if d is None:
            return True
        if d.classes or d.added or d.module_names or d.module_other or any(
                maps.runs_at_import(path, *analyse.body_range(n)) for _, n in d.functions):
            return True
    return False


TPC_ROWS = Path(__file__).resolve().parent.parent / "maps" / "tpc_rows.pkl.z"


def _load_traces(maps, attr, path):
    if not hasattr(maps, attr):
        import pickle
        import zlib
        setattr(maps, attr, {k: (set(f), set(r)) for k, (f, r) in
                             pickle.loads(zlib.decompress(path.read_bytes())).items()}
                if path.exists() else None)
    return getattr(maps, attr)


def tpc_reachable(files, overrides, repo, maps, import_diff, suites, rows=False):
    """TPC queries whose preparation (rows=True: row-by-row evaluation) can see the
    patch (see tpc_check.py --query-traces / --rows-traces): it calls a changed
    function, a lambda of a changed class body, or a method named like an added
    one, or reads a changed import-time value. None means it cannot be narrowed."""
    all_traces = _load_traces(maps, "_tpc_rows", TPC_ROWS) if rows else \
        _load_traces(maps, "_tpc_queries", TPC_QUERIES)
    if all_traces is None:
        return None
    traces = {k: v for k, v in all_traces.items() if k.split()[0] in suites}
    funcs, files_with_body_change, added = [], set(), set()
    at_import = False
    for f in files:
        path = f["new"]
        old = (repo / path).read_text() if (repo / path).exists() else ""
        d = analyse.diff_python(old, overrides[path], patchlib.changed_old_lines(f["hunks"]),
                                patchlib.changed_new_lines(f["hunks"]))
        if d is None:
            return None
        if d.same:
            continue
        funcs += ["%s:%s" % (path, q) for q, _ in d.functions]
        if d.classes or d.module_names or d.module_other:
            files_with_body_change.add(path)
        added |= {q.rsplit(".", 1)[-1] for q in d.added}
        at_import = at_import or bool(d.classes or d.added or d.module_names or d.module_other) or any(
            maps.runs_at_import(path, *analyse.body_range(n)) for _, n in d.functions)
    changed_reads, any_read = set(), set()
    if at_import:
        changed = import_diff()
        if changed is None:
            return None
        tracked = (_reads(maps) or {}).get("keys", {})
        fnames = {x.rsplit(".", 1)[-1] for x in funcs}
        for key in changed:
            name = key.split(":", 1)[1].rsplit(".", 1)[-1]
            items = DIFF_ITEMS.get(key)
            if key not in tracked and name not in fnames:
                return None  # an untraced value (a module constant, a class...)
            if items is None:
                any_read.add(key)
            else:
                changed_reads |= {(key, i) for i in items}
                changed_reads.add((key, "<all>"))
    out = set()
    for k, (called, reads) in traces.items():
        hit = any(c == fn or c.startswith(fn + ".") for fn in funcs for c in called)
        hit = hit or any(c.split(":", 1)[0] in files_with_body_change and "<" in c for c in called)
        hit = hit or any(c.rsplit(".", 1)[-1] in added for c in called)
        hit = hit or bool(changed_reads & reads) or any(r[0] in any_read for r in reads)
        if hit:
            out.add(k)
    return out


def tpc_droppable(files, sel, overrides, repo, maps, notes, import_diff, cost):
    """The TPC executor tests in sel that provably still pass (see tpc_check.py)."""
    ids = {t["id"]: t["number"] for t in maps.tests}
    present = {ids[k]: v for k, v in TPC_TESTS.items() if ids.get(k) in sel}
    if not present or not TPC_BASELINE.exists() or not TPC_RUNTIME.exists():
        return set()
    if any(not (f["new"].startswith("sqlglot/") and f["new"].endswith(".py")) or f["old"] != f["new"]
           for f in files):
        return set()  # tests, fixtures or other files changed too: not covered by the check
    if cost(sel) - cost(sel - set(present)) < TPC_WORTH_S * 1000:
        return set()
    trace = json.loads(TPC_RUNTIME.read_text())
    if isinstance(trace, list):  # older trace: functions only, no reads
        trace = {"functions": trace, "reads": None}
    runtime = {}
    for entry in trace["functions"]:
        path, qual = entry.split(":", 1)
        runtime.setdefault(path, set()).add(qual.replace(".<locals>", ""))
    method_names = {q.rsplit(".", 1)[-1] for quals in runtime.values() for q in quals}
    row_reads = {tuple(r) for r in trace["reads"]} if trace.get("reads") is not None else None
    row_keys = {k for k, _ in row_reads} if row_reads is not None else None
    rows_safe = True     # False: the row-by-row evaluation may change and cannot be checked
    rows_unsure = False  # True: it may change, unless the queries it touches are rerun
    for f in files:
        path = f["new"]
        if path.startswith("sqlglot/executor/"):
            rows_safe = False
            continue
        old = (repo / path).read_text() if (repo / path).exists() else ""
        ranges = patchlib.changed_old_lines(f["hunks"])
        d = analyse.diff_python(old, overrides[path], ranges, patchlib.changed_new_lines(f["hunks"]))
        if d is None:
            return set()
        if d.same:
            continue
        used = runtime.get(path, set())
        for q, _ in d.functions:
            if any(u == q or u.startswith(q + ".") for u in used):
                rows_unsure = True
        if any(q.rsplit(".", 1)[-1] in method_names for q in d.added):
            rows_unsure = True  # a new method may override one rows call
        at_import = bool(d.classes or d.added or d.module_names or d.module_other) or any(
            maps.runs_at_import(path, *analyse.body_range(n)) for _, n in d.functions)
        if at_import and not rows_read_nothing_changed(import_diff, row_reads, row_keys, d, maps):
            rows_unsure = True
    execute_keys = []
    if rows_safe and rows_unsure:
        # rerun just the queries whose rows touch the change, on the real data
        affected = tpc_reachable(files, overrides, repo, maps, import_diff, ["tpcds", "tpch"], rows=True)
        if affected is None:
            rows_safe = False
        else:
            execute_keys = sorted(affected)
    if not rows_safe and all(rt for _, rt in present.values()):
        return set()
    fixtures = TPC_FIXTURES or str(repo / "tests" / "fixtures")
    suites = sorted({suite for n, (suite, rt) in present.items() if rows_safe or not rt})
    only = tpc_reachable(files, overrides, repo, maps, import_diff, suites)
    if only is not None and not only and not execute_keys:
        changed_suites = set()  # no query's preparation can see the change
    else:
        worker = CURRENT[0]
        if worker is None:
            return set()
        result = worker.call({"op": "tpc", "fixtures": fixtures, "baseline": str(TPC_BASELINE),
                              "suites": suites, "only": sorted(only) if only is not None else None,
                              "execute": execute_keys},
                             15, notes, "tpc check")
        if not result or not result.get("ok"):
            return set()
        changed_suites = {k.split()[0] for k in result["changed"]}
    notes.append(("tpc queries rechecked", "all" if only is None else len(only)))
    drop = {n for n, (suite, rt) in present.items()
            if suite in suites and suite not in changed_suites and (rows_safe or not rt)}
    notes.append(("tpc unchanged", sorted(drop), sorted(changed_suites)))
    return drop


def _reads(maps):
    if not hasattr(maps, "_reads"):
        maps._reads = None
        if READS.exists():
            import pickle
            import zlib
            maps._reads = pickle.loads(zlib.decompress(READS.read_bytes()))
    return maps._reads


def selection_by_reads(changed, items, path, d, maps, notes):
    """Tests that read what changed at import (see tools/read_plugin.py), or None
    when something changed that the read map cannot follow.

    changed: attribute keys whose value differs after the patched import
    items:   for changed dicts and sets, which of their items differ
    """
    reads = _reads(maps)
    if reads is None:
        return None
    out = set(reads["unknown"])
    funcs = {q.rsplit(".", 1)[-1]: n for q, n in d.functions}
    scalars = set()
    for key in changed:
        i = reads["keys"].get(key)
        if key.rsplit(".", 1)[-1].split(":")[-1] in READ_BY_C:
            scalars.add(key.rsplit(".", 1)[-1].split(":")[-1])
        if i is not None:
            if i in reads["collect"]:
                notes.append(("read at collection", key))
                return None
            out |= reads["all"].get(i, frozenset())  # whole-container and scalar reads
            by_item = reads["items"].get(i, {})
            changed_items = items.get(key)
            if changed_items is None:  # no item-level answer: every reader
                for v in by_item.values():
                    out |= v
            else:
                for item in changed_items:
                    out |= by_item.get(item, frozenset())
            continue
        name = key.split(":", 1)[1].rsplit(".", 1)[-1]
        if name in funcs:
            # an edited function seen through a changed default or closure:
            # every call runs its body
            out |= maps.covering(path, *analyse.body_range(funcs[name]))
        elif name.startswith("__"):
            notes.append(("read map cannot follow", key))
            return None
        else:
            scalars.add(name)  # a plain value: read only by lines that name it
    if scalars:
        out |= maps.referencing(scalars) | tests_naming(scalars, maps)
    notes.append(("import-time reads", len(changed), sorted(scalars)[:4], len(out)))
    return out


def python_change(path, old, new, maps, notes, changed_ranges=(), new_lines=None, import_diff=None):
    d = analyse.diff_python(old, new, {lo for lo, _ in changed_ranges} if changed_ranges else None, new_lines)
    if d is None:
        notes.append(("syntax error -> all", path))
        return set(maps.all)
    if d.same:
        notes.append(("no code change", path))
        return set()
    sel = set()
    fam = family(path, maps)
    broad = False  # an import-time change: needs the dialect family, or everything

    for q, node in d.functions:
        lo, hi = analyse.body_range(node)
        if changed_ranges:
            in_func = [r[0] for r in changed_ranges if lo <= r[0] <= hi]
            if in_func:
                stmt_ranges = analyse.stmt_ranges_in(node, in_func)
                if stmt_ranges:
                    for s_lo, s_hi in stmt_ranges:
                        sel |= maps.covering(path, s_lo, s_hi)
                else:
                    sel |= maps.covering(path, lo, hi)
            else:
                sel |= maps.covering(path, lo, hi)
        else:
            sel |= maps.covering(path, lo, hi)
        if maps.runs_at_import(path, lo, hi):
            broad = True
            notes.append(("function runs at import", q))
        else:
            notes.append(("function", q))
    import_time = bool(d.classes or d.added or d.module_names or d.module_other) or any(
        maps.runs_at_import(path, *analyse.body_range(n)) for _, n in d.functions)
    if import_time and import_diff is not None:
        # the rules below must assume import-time effects reach far. Checking the
        # patched import directly costs about a second of CPU, so only do it when
        # it could save more than that
        narrow = set(sel)
        for lo, hi in changed_ranges:
            narrow |= maps.covering(path, lo, hi)
        own = []
        broad = python_change(path, old, new, maps, own, changed_ranges, new_lines)
        # Shared code must always be checked: values derived from it elsewhere at import
        # are invisible to the rules below (JSONFormat._sql_names feeds Parser.FUNCTIONS).
        # A dialect's class settings are only read by its own family, which they cover.
        structural = bool(d.classes or d.added or d.module_names or d.module_other) and fam is None
        if not structural and maps.seconds(broad - narrow) < CHECK_WORTH_S:
            notes.extend(own)
            return broad
        changed = import_diff()
        if changed == []:
            # nothing built at import differs: only tests running the changed lines can notice
            notes.append(("import state unchanged", path))
            return narrow
        if changed:
            by_reads = selection_by_reads(changed, DIFF_ITEMS, path, d, maps, notes)
            if by_reads is not None:
                by_reads |= narrow
                if maps.seconds(by_reads) < maps.seconds(broad):  # both are safe: take the cheaper
                    return by_reads
        if changed and not d.added and NAME_NARROW:
            # specific values built at import changed (a DuckDB SQL template the parser
            # builds, Parser.SUPPORTS_DIRECTED_JOIN): add the tests that run code naming
            # them, or whose own code names them. A changed function shows up here when
            # a default or closure changed: every call runs its body, already covered.
            funcs = {q.rsplit(".", 1)[-1]: n for q, n in d.functions}
            names, extra = set(), set()
            for key in changed:
                name = key.split(":", 1)[1].rsplit(".", 1)[-1]
                if name in funcs:
                    extra |= maps.covering(path, *analyse.body_range(funcs[name]))
                else:
                    names.add(name)
            if all(not n.startswith("__") and len(maps.files_naming(n)) <= 4 for n in names):
                by_name = narrow | extra | maps.referencing(names) | tests_naming(names, maps)
                if maps.seconds(by_name) < maps.seconds(broad):  # both are safe: take the cheaper
                    notes.append(("import-time values changed", sorted(names)))
                    return by_name
        if changed:
            # The rules below follow the changed class or setting by name. Values derived
            # from it elsewhere at import (Parser.FUNCTIONS is built from every
            # _sql_names) are reached without naming it, so they need everything.
            mod = path[:-3].replace("/", ".")
            mod = mod[:-len(".__init__")] if mod.endswith(".__init__") else mod
            owners = ["%s:%s" % (mod, q) for q, _ in d.classes + d.functions] \
                + ["%s:%s" % (mod, n) for n in d.module_names]
            fam_mods = set()
            if fam is not None:
                fam_mods = {"sqlglot.%s.%s" % (dd, dialect)
                            for dialect in _subclass_dialects(_dialect_of(path), maps) for dd in FAMILY_DIRS}
            foreign = [k for k in changed if k.split(":", 1)[0] not in fam_mods
                       and not any(k == o or k.startswith(o + ".") for o in owners)]
            if foreign:
                notes.append(("import state changed elsewhere -> all", foreign[:3]))
                return set(maps.all)
        notes.extend(own)
        return broad
    specific = {n for n in d.class_names if maps.is_specific(n, path)}
    for q, node in d.classes:
        notes.append(("class body", q))
        broad = True
    if d.classes:
        # lines the patch touched inside class bodies: lambdas there run per test
        for lo, hi in changed_ranges:
            sel |= maps.covering(path, lo, hi)
    if d.classes and fam is not None and d.class_names and specific == d.class_names \
            and not d.class_unnamed and not d.added and not d.module_names and not d.module_other \
            and not any(maps.runs_at_import(path, *analyse.body_range(n)) for _, n in d.functions):
        # e.g. WEEK_OFFSET in TSQL: narrow by both referencing and family_covering, pick smaller
        refs = maps.referencing(specific)
        candidate_refs = fam & refs
        fcov = family_covering(path, maps) or set()
        use_cov = fcov and (not candidate_refs or len(fcov) < len(candidate_refs))
        best = fcov if use_cov else candidate_refs
        sel |= best
        notes.append(("dialect covering narrow" if use_cov else "specific dialect setting",
                      _dialect_of(path) if use_cov else sorted(specific)))
        if sel:
            fam, broad = None, False   # precise enough: do not widen to the whole dialect
    elif d.classes and fam is not None and d.class_names and not d.class_unnamed \
            and not d.added and not d.module_names and not d.module_other \
            and not any(maps.runs_at_import(path, *analyse.body_range(n)) for _, n in d.functions):
        # e.g. SHOW_PARSERS / KEYWORDS: narrow to tests that actually ran dialect code.
        # Try two independent subsets of fam and pick the smaller:
        # (1) fam & refs(names): tests that ran code mentioning the changed names
        # (2) family_covering: tests that ran any line in the dialect family's source files
        # family_covering is safe for pure attr changes: a failing test must call dialect-
        # specific code, which lives in the dialect's own files.
        refs = maps.referencing(d.class_names)
        candidate_refs = fam & refs
        fcov = family_covering(path, maps) or set()
        use_cov = fcov and (not candidate_refs or len(fcov) < len(candidate_refs))
        best = fcov if use_cov else candidate_refs
        if best:
            sel |= best
            if use_cov:
                notes.append(("dialect covering narrow", _dialect_of(path)))
            else:
                notes.append(("dialect class attrs narrow", sorted(d.class_names)))
            fam, broad = None, False
    elif d.classes and fam is None:
        # a shared class changed at import time: follow uses of the class and of specific names
        names = specific | {q.rsplit(".", 1)[-1] for q, _ in d.classes}
        refs = maps.referencing(names)
        if not refs:
            notes.append(("class change with no known users -> all", path))
            return set(maps.all)
        sel |= refs
        if path.startswith("sqlglot/expressions/"):
            # expressions are also built from SQL text by name (APPROX_DISTINCT(...) ->
            # ApproxDistinct.from_arg_list) in generic code that never names the class
            sql = sql_names_of({q.rsplit(".", 1)[-1] for q, _ in d.classes}, maps)
            by_sql = tests_naming(sql, maps, ignore_case=True) | fixture_readers(sql, maps)
            notes.append(("expression sql names", sorted(sql)[:6], len(by_sql)))
            sel |= by_sql
    for q in d.added:
        owner = q.rsplit(".", 1)[0] if "." in q else ""
        node = d.units.get(owner)
        if node is not None:
            sel |= maps.covering(path, node.lineno, node.end_lineno)
        notes.append(("added", q))
        broad = True
    if d.module_names:
        if fam is not None:
            sel |= maps.covering(path)
        else:
            sel |= maps.referencing(d.module_names)
        notes.append(("module names", sorted(d.module_names)))
        broad = True
    if d.module_other:
        if fam is not None:
            notes.append(("module statement in dialect", path))
            sel |= fam
        else:
            notes.append(("module statement -> all", path))
            return set(maps.all)

    if broad:
        if fam is not None:
            sel |= fam
        elif d.module_names or d.added or any(maps.runs_at_import(path, *analyse.body_range(n))
                                              for _, n in d.functions):
            if d.module_names and not d.added and not any(
                    maps.runs_at_import(path, *analyse.body_range(n)) for _, n in d.functions):
                # Only module-level names changed: trust referencing to find all relevant tests
                notes.append(("module names core", sorted(d.module_names)))
            else:
                # shared machinery that runs at import: anything could depend on it
                notes.append(("import-time core change -> all", path))
                return set(maps.all)
    return sel


def changed_test_methods(d):
    """Names of the test methods a test-module diff changes, or None when it
    changes anything else (a helper, setUp, a class body, module code)."""
    if d.classes or d.module_names or d.module_other:
        return None
    methods = set()
    for q in [q for q, _ in d.functions] + list(d.added):
        parts = q.split(".")
        if len(parts) >= 2 and parts[1].startswith("test"):
            methods.add(parts[1])
        elif parts[0].startswith("test"):
            methods.add(parts[0])  # a module-level test function
        else:
            return None
    return methods


def test_file_change(path, old, new, maps, notes):
    """A test module changed. Edits inside test methods affect that file only;
    anything else (helpers, base classes such as Validator in test_dialect.py)
    also reaches every test file that imports this one."""
    own = maps.by_file.get(path)
    if not own or not path.split("/")[-1].startswith("test_"):
        imp = importers(path, maps)
        if imp:
            notes.append(("test helper -> importers", path, len(imp)))
            return imp
        notes.append(("test helper -> all", path))
        return set(maps.all)
    d = analyse.diff_python(old, new)
    if d is None:
        notes.append(("test file syntax error", path))
        return set(own) | importers(path, maps)
    if d.same:
        notes.append(("no code change", path))
        return set()
    # Edits inside test methods (or functions nested in them) reach only those
    # tests: unittest runs each method on its own. A test inherited by a class
    # elsewhere runs there too, so match the method name in importing files as well.
    methods = changed_test_methods(d)
    if methods is None:
        notes.append(("test file", path, "shared code"))
        return set(own) | importers(path, maps)
    files = {path} | {t["file"] for t in maps.tests if t["number"] in importers(path, maps)}
    sel = {t["number"] for t in maps.tests if t["file"] in files
           and t["id"].rsplit("::", 1)[-1] in methods}
    notes.append(("test file", path, "tests", sorted(methods)[:4], len(sel)))
    return sel


_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def sql_names_of(classes, maps):
    """SQL names that build these expression classes or their subclasses: their
    _sql_names (or the derived NAME), and dialect parser entries such as
    "NDV": exp.ApproxDistinct.from_arg_list or "X": build_x(exp.ApproxDistinct)."""
    if not hasattr(maps, "_all_text"):
        maps._all_text = "\n".join("\n".join(v) for v in maps.source_lines().values())
    text = maps._all_text
    todo, found = list(classes), set(classes)
    while todo:  # subclasses inherit the change
        cls = todo.pop()
        for m in re.finditer(r"^\s*class (\w+)\(([^)]*)\)", text, re.M):
            if m.group(1) not in found and re.search(r"\b%s\b" % re.escape(cls), m.group(2)):
                found.add(m.group(1))
                todo.append(m.group(1))
    names = set()
    for cls in found:
        names.add(_CAMEL.sub("_", cls).upper())
        m = re.search(r"^\s*class %s\(.*?(?=^\s*class |\Z)" % re.escape(cls), text, re.M | re.S)
        if m:
            sn = re.search(r"_sql_names\s*=\s*\[([^\]]*)\]", m.group(0))
            if sn:
                names |= set(re.findall(r"[\"']([^\"']+)[\"']", sn.group(1)))
        for m in re.finditer(r"[\"']([A-Za-z0-9_]+)[\"']\s*:\s*[^\n]*\b%s\b" % re.escape(cls), text):
            names.add(m.group(1))
    return names


def fixture_readers(names, maps):
    """Tests that read a fixture file mentioning one of these names (any case)."""
    word = re.compile(r"\b(%s)\b" % "|".join(map(re.escape, sorted(names))), re.I)
    if not hasattr(maps, "_fixture_text"):
        maps._fixture_text = {}
        for path in maps.opened:
            try:
                maps._fixture_text[path] = (maps.repo / path).read_text(errors="replace")
            except OSError:
                maps._fixture_text[path] = None  # not shipped: assume it might mention them
    out = set()
    for path, users in maps.opened.items():
        text = maps._fixture_text.get(path)
        if text is None or word.search(text):
            out |= users
    return out


def tests_naming(names, maps, ignore_case=False):
    """Tests whose own code names one of these identifiers: the test method, or
    every test in the file (and its importers) when a helper names it."""
    import ast
    word = re.compile(r"\b(%s)\b" % "|".join(map(re.escape, sorted(names))), re.I if ignore_case else 0)
    out = set()
    for p in maps.repo.glob("tests/**/*.py"):
        text = p.read_text()
        if not word.search(text):
            continue
        rel = str(p.relative_to(maps.repo))
        hit_lines = {i for i, line in enumerate(text.split("\n"), 1) if word.search(line)}
        ids = {t["id"]: t["number"] for t in maps.tests if t["file"] == rel}
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        covered = set()
        for node in ast.walk(tree) if tree else ():
            if isinstance(node, ast.ClassDef):
                for f in node.body:
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test"):
                        mine = {i for i in hit_lines if f.lineno <= i <= f.end_lineno}
                        if mine:
                            covered |= mine
                            tid = "%s::%s::%s" % (rel, node.name, f.name)
                            if tid in ids:
                                out.add(ids[tid])
        if hit_lines - covered:
            out |= maps.by_file.get(rel, set()) | importers(rel, maps)
    return out


def importers(path, maps):
    """Tests in any test file that imports this test module."""
    module = path[:-3].replace("/", ".")
    pat = re.compile(r"^\s*(from\s+%s\s+import|import\s+%s\b)" % (re.escape(module), re.escape(module)), re.M)
    out = set()
    for p in maps.repo.glob("tests/**/*.py"):
        rel = str(p.relative_to(maps.repo))
        if rel != path and pat.search(p.read_text()):
            out |= maps.by_file.get(rel, set())
    return out


def candidates(patch_text, repo, maps):
    """Return (set of tests, notes)."""
    sel, notes = set(), []
    files = patchlib.parse(patch_text)
    overrides, checkable = {}, True
    for f in files:
        if f["new"].startswith("sqlglot/") and f["new"].endswith(".py"):
            if f["new"] == "/dev/null" or f["old"] != f["new"] and f["old"] != "/dev/null":
                checkable = False  # deleted or renamed module: leave it to the broad rules
                continue
            src = repo / f["new"]
            overrides[f["new"]] = patchlib.apply(src.read_text() if src.exists() else "", f["hunks"])
        elif f["old"].startswith("sqlglot/"):
            checkable = False
    CURRENT[0] = PatchedWorker(repo, overrides) if checkable else None
    try:
        return _candidates(files, repo, maps, sel, notes, overrides, checkable)
    finally:
        if CURRENT[0] is not None:
            CURRENT[0].close()
        CURRENT[0] = None


def _candidates(files, repo, maps, sel, notes, overrides, checkable):
    memo = []

    def import_diff():
        if not memo:
            memo.append(import_state_diff(repo, overrides, notes) if checkable else None)
        return memo[0]

    for f in files:
        path = f["old"] if f["old"] != "/dev/null" else f["new"]
        src = repo / path
        old = src.read_text() if src.exists() else ""
        if path.startswith("sqlglot/") and path.endswith(".py"):
            lines = patchlib.changed_old_lines(f["hunks"])
            ranges = [(ln, ln) for ln in lines]
            sel |= python_change(path, old, patchlib.apply(old, f["hunks"]), maps, notes, ranges,
                                 patchlib.changed_new_lines(f["hunks"]), import_diff)
        elif path.startswith("tests/") and path.endswith(".py"):
            sel |= test_file_change(path, old, patchlib.apply(old, f["hunks"]), maps, notes)
        elif path.startswith("tests/"):
            users = maps.opened.get(path)
            if users and path not in maps.unattributed:
                sel |= users
                notes.append(("fixture", path, len(users)))
            else:
                sel |= maps.all
                notes.append(("fixture with unknown readers -> all", path))
        elif path.startswith("sqlglot/"):
            # package data: nothing in sqlglot reads files at run time (py.typed etc.)
            notes.append(("package non-python file", path))
        elif path.split("/")[0] in PACKAGING:
            # read only by the packaging tests (setuptools runs setup.py / pyproject.toml)
            users = set().union(*(maps.by_file.get(f, set()) for f in PACKAGING_TESTS))
            sel |= users
            notes.append(("packaging file", path, len(users)))
        else:
            # docs, README, CHANGELOG, CI config, benchmarks: no test reads them
            notes.append(("file no test reads", path))
    import split

    def cost(nums):
        dur = {n: int(maps.secs[n] * 1000 + 0.5) for n in nums}
        ws = split.best_split(nums, dur)
        return max(sum(dur[x] for x in w) for w in ws) + split.WORKER_MS * len(ws) if ws else 0

    code = [f for f in files if f["new"].startswith("sqlglot/") and f["new"].endswith(".py")]
    rest = [f for f in files if f not in code]

    def rest_cannot_reach(test_file, test_ids):
        """No changed file other than sqlglot source can affect these tests of
        test_file: in that file only other test methods changed, and no test
        module it imports nor fixture they read changed. (sqlglot source is what
        the checks themselves evaluate.)"""
        mine = maps.by_file.get(test_file, set())
        for f in rest:
            path = f["new"] if f["new"] != "/dev/null" else f["old"]
            if path == test_file:
                if f["old"] != f["new"]:
                    return False
                src = (repo / path).read_text() if (repo / path).exists() else ""
                d = analyse.diff_python(src, patchlib.apply(src, f["hunks"]))
                methods = changed_test_methods(d) if d is not None else None
                if methods is None or methods & {t.rsplit("::", 1)[-1] for t in test_ids}:
                    return False
                continue
            if path.startswith("tests/"):
                if path.endswith(".py"):
                    if importers(path, maps) & mine or path.endswith("__init__.py"):
                        return False
                elif path in maps.unattributed or maps.opened.get(path, set()) & mine:
                    return False
        return True

    # the import-state snapshot has to be taken before other checks run code in the
    # shared worker: ask for it now if the TPC check may need it
    if sel and checkable and code and any(t["id"] in TPC_TESTS and t["number"] in sel for t in maps.tests) \
            and import_time_change(code, overrides, repo, maps):
        import_diff()
    if sel and checkable and rest_cannot_reach("tests/test_parser.py", {MAXNODES_TEST}):
        sel -= max_nodes_unaffected(sel, overrides, repo, maps, notes)
    if sel and checkable and TPC_ENABLED and rest_cannot_reach("tests/test_executor.py", set(TPC_TESTS)):
        sel -= tpc_droppable(code, sel, overrides, repo, maps, notes, import_diff, cost)
    if sel and checkable and overrides and not rest:
        sel -= passing_here(sel, overrides, repo, maps, notes) if RUN_ENABLED else set()
    return sel, notes


def max_nodes_unaffected(sel, overrides, repo, maps, notes):
    """{test_max_nodes} when it is selected, leaving it out saves enough, and
    app/maxnodes_check.py proves the patch leaves everything it checks unchanged."""
    import split
    n = next((t["number"] for t in maps.tests if t["id"] == MAXNODES_TEST), None)
    if n is None or n not in sel:
        return set()

    def cost(nums):
        dur = {x: int(maps.secs[x] * 1000 + 0.5) for x in nums}
        ws = split.best_split(nums, dur)
        return max(sum(dur[x] for x in w) for w in ws) + split.WORKER_MS * len(ws) if ws else 0

    if cost(sel) - cost(sel - {n}) < MAXNODES_WORTH_S * 1000:
        return set()
    worker = CURRENT[0]
    if worker is None:
        return set()
    result = worker.call({"op": "max_nodes"}, 15, notes, "max_nodes check")
    passes = bool(result and result.get("ok") and result.get("passes"))
    notes.append(("max_nodes passes" if passes else "max_nodes may fail", (result or {}).get("why", "")[:60]))
    return {n} if passes else set()


TEST_SERVER = Path(__file__).resolve().with_name("test_server.py")
_SERVER = [None, False]  # the shared test server process, and whether it died


def _server_call(repo, request, timeout, notes):
    """Ask the shared test server (app/test_server.py); None if it cannot help."""
    import select
    if _SERVER[1]:
        return None
    try:
        if _SERVER[0] is None:
            import atexit
            env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
            _SERVER[0] = subprocess.Popen([sys.executable, str(TEST_SERVER), str(repo)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True, env=env)
            atexit.register(lambda: _SERVER[0] and _SERVER[0].kill())
        _SERVER[0].stdin.write(json.dumps(request) + "\n")
        _SERVER[0].stdin.flush()
        ready, _, _ = select.select([_SERVER[0].stdout], [], [], timeout)
        line = _SERVER[0].stdout.readline() if ready else ""
        if not line:
            raise TimeoutError("test server: no answer in %ss" % timeout)
        out = json.loads(line)
        if "cannot" in out:
            notes.append(("test server cannot", str(out["cannot"])[:80]))
            return None
        return out
    except Exception as error:
        notes.append(("test server failed", repr(error)[:100]))
        if _SERVER[0] is not None:
            _SERVER[0].kill()
        _SERVER[1] = True
        return None


def body_only_changes(overrides, repo, maps):
    """{path: [qualnames]} when the patch changes nothing but function bodies
    (same signatures, defaults, decorators; nothing that runs at import), so
    swapping in the new bodies reproduces the patched program exactly; else None."""
    import ast
    out = {}
    for path, new_src in overrides.items():
        old_src = (repo / path).read_text()
        d = analyse.diff_python(old_src, new_src)
        if d is None:
            return None
        if d.same:
            continue
        if d.classes or d.added or d.module_names or d.module_other or not d.functions:
            return None
        new_units = analyse._units(ast.parse(new_src))
        for q, node in d.functions:
            n2 = new_units.get(q)
            if n2 is None or type(n2) is not type(node) or maps.runs_at_import(path, *analyse.body_range(node)):
                return None
            sig = lambda f: ast.dump(ast.Module(body=[], type_ignores=[])) + "|".join(
                ast.dump(x) for x in [f.args] + f.decorator_list + ([f.returns] if f.returns else []))
            if sig(node) != sig(n2):
                return None
        out[path] = [q for q, _ in d.functions]
    return out


CALLS = Path(__file__).resolve().parent.parent / "maps" / "calls.pkl.z"


def _calls_by_test(maps):
    """test id -> [(function ids, seconds)] of its Validator helper calls (maps/calls.pkl.z)."""
    if not hasattr(maps, "_calls"):
        maps._calls = None
        if CALLS.exists():
            import pickle
            import zlib
            m = pickle.loads(zlib.decompress(CALLS.read_bytes()))
            by_test = {}
            for (test, _, _, _), (fids, secs) in m["calls"].items():
                by_test.setdefault(test, []).append((frozenset(fids), secs))
            maps._calls = (m["names"], by_test, m["lazy"])
    return maps._calls


def run_estimate(sel, bodies, maps):
    """Recorded seconds a run of sel costs when the server skips the helper calls
    that cannot see the changed function bodies (the rest of each test runs)."""
    m = _calls_by_test(maps)
    ids = {t["number"]: t["id"] for t in maps.tests}
    if m is None or not bodies:
        return maps.seconds(sel)
    names, by_test, lazy = m
    changed = set()
    for path, quals in bodies.items():
        for q in quals:
            p = "%s:%s" % (path, q)
            changed |= {i for i, n in enumerate(names) if n == p or n.startswith(p + ".")}
    if changed & lazy:
        return maps.seconds(sel)
    total = 0.0
    for n in sel:
        calls = by_test.get(ids[n])
        if not calls:
            total += maps.secs[n]
            continue
        all_s = sum(s for _, s in calls) or 1e-9
        hit_s = sum(s for f, s in calls if f & changed)
        total += maps.secs[n] * hit_s / all_s + 0.01  # the rest of the test body still runs
    return total


def passing_here(sel, overrides, repo, maps, notes):
    """The selected tests that pass when run here against the patched source,
    when running them costs less than leaving them out saves (app/run_tests.py)."""
    import split
    import run_tests
    worker = CURRENT[0]
    if worker is None:
        return set()
    ids = {t["number"]: t["id"] for t in maps.tests}
    files = {t["number"]: t["file"] for t in maps.tests}
    if not all(run_tests.runnable(ids[n]) and files[n] not in RUN_SKIP_FILES for n in sel):
        return set()  # the commit would still need a worker: running buys little
    bodies = body_only_changes(overrides, repo, maps)
    estimate = run_estimate(sel, bodies, maps) if bodies else maps.seconds(sel)
    if estimate > (RUN_MAX_S if bodies else RUN_MAX_FULL_S):
        return set()

    def cost(nums):
        dur = {n: int(maps.secs[n] * 1000 + 0.5) for n in nums}
        ws = split.best_split(nums, dur)
        return max(sum(dur[x] for x in w) for w in ws) + split.WORKER_MS * len(ws) if ws else 0

    if cost(sel) < RUN_WORTH_S * 1000:
        return set()
    first = sorted({path.rsplit("/", 1)[-1][:-3] for path in overrides} - {"__init__", "core", "dialect"})
    request = {"tests": [ids[n] for n in sorted(sel)], "seconds": {ids[n]: maps.secs[n] for n in sel},
               "first": first}
    timeout = RUN_TIMEOUT_S + 5 * maps.seconds(sel)
    result = None
    if bodies:  # cheap: the shared server swaps the new bodies in, skips unaffected calls
        result = _server_call(repo, dict(request, files={p: overrides[p] for p in bodies},
                                         functions=bodies, skip_calls=True), timeout, notes)
        if result is not None:
            result["ok"] = True
    if result is None:  # the commit's own patched worker
        result = worker.call(dict(request, op="run_tests"), timeout, notes, "test run")
    if not result or not result.get("ok"):
        return set()
    by_id = {v: k for k, v in ids.items()}
    passed = {by_id[t] for t in result.get("passed", ()) if t in by_id}
    notes.append(("ran here", len(sel), "passed", len(passed), "failed", len(result.get("failed", ())),
                  "est %.2f" % estimate, "cpu %s" % result.get("cpu"), "calls %s" % result.get("calls")))
    return passed

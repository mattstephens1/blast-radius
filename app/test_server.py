"""A long-lived process that runs a commit's selected tests cheaply.

Running a few tests per commit is cheap; starting Python, importing sqlglot and
importing test modules (test_optimizer pulls in pandas and DuckDB) per commit
is not. This process imports them once, unpatched, and for each request forks a
child that swaps in the patched code and runs the tests (run_tests.run_here).

The swap replaces the __code__ of the functions the patch changes, so every
reference to them (methods, entries of dispatch tables, imported names) runs the
new body. It is only used when the patch changes nothing but function bodies:
same signature, defaults, decorators, and same closure variables. Anything else,
or any function it cannot locate exactly, answers "cannot", and the selector
falls back to its per-commit worker.

    stdin:  one JSON request per line:
            {"files": {path: patched source}, "functions": {path: [qualname]},
             "tests": [...], "seconds": {...}, "first": [...]}
    stdout: one JSON answer per line: run_tests.run_here's result, or {"cannot": why}
"""
# sqlglot turns on extra validation when it is imported under a test runner
# (expressions/core.py: UNITTEST = "unittest" in sys.modules or "pytest" in
# sys.modules). Import unittest first so every check sees what the tests see.
import unittest  # noqa: F401
import importlib
import json
import os
import pickle
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def _code_by_qualname(code, out):
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out.setdefault(const.co_qualname, const)
            _code_by_qualname(const, out)
    return out


def _target(module, qualname):
    """The function object a qualname names in a module (no <locals> parts)."""
    obj = module
    for part in qualname.split("."):
        obj = obj.__dict__.get(part) if isinstance(obj, (type, types.ModuleType)) else None
        if obj is None:
            return None
        if isinstance(obj, (staticmethod, classmethod)):
            obj = obj.__func__
    return obj if isinstance(obj, types.FunctionType) and obj.__code__.co_qualname == qualname else None


def swap(files, functions):
    """Give the changed functions their patched bodies. Returns None, or why not."""
    for path, quals in functions.items():
        name = path[:-3].replace("/", ".")
        module = sys.modules.get(name) or importlib.import_module(name)
        new = _code_by_qualname(compile(files[path], module.__file__, "exec", dont_inherit=True), {})
        for q in quals:
            top = q.split(".")
            # a nested function's code lives inside its enclosing function's code
            for i in range(len(top), 0, -1):
                f = _target(module, ".".join(top[:i]))
                if f is not None:
                    break
            if f is None:
                return "cannot locate %s" % q
            code = new.get(f.__code__.co_qualname)
            if code is None or code.co_freevars != f.__code__.co_freevars:
                return "cannot swap %s" % q
            f.__code__ = code
    return None


HELPERS = ("validate_identity", "validate_all", "validate_transpile")
CALLS = os.path.join(os.path.dirname(HERE), "maps", "calls.pkl.z")
STATS = {"skipped": 0, "ran": 0, "unknown": 0}


def _bare_sites(repo, rel, cache):
    """Line ranges of statements that call a Validator helper and drop the result."""
    if rel not in cache:
        import ast
        ranges = []
        try:
            tree = ast.parse(open(os.path.join(repo, rel)).read())
        except (OSError, SyntaxError):
            tree = None
        for node in ast.walk(tree) if tree else ():
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                f = node.value.func
                if isinstance(f, ast.Attribute) and f.attr in HELPERS and isinstance(f.value, ast.Name) \
                        and f.value.id == "self":
                    ranges.append((node.lineno, node.end_lineno))
        cache[rel] = ranges
    return cache[rel]


def skip_unaffected_calls(repo, calls_map, functions):
    """Wrap the Validator helpers so a call that, in the offline call map, executed
    none of the changed functions returns at once. Only for bare-statement calls
    (their result is dropped), known to the map, outside a helper. Returns how many
    calls it may skip, or None if skipping is not safe for this patch."""
    import run_tests
    names = calls_map["names"]
    changed = set()
    for path, quals in functions.items():
        for q in quals:
            p = "%s:%s" % (path, q)
            changed |= {i for i, n in enumerate(names) if n == p or n.startswith(p + ".")}
    if changed & calls_map["lazy"]:
        return None
    from tests.dialects.test_dialect import Validator
    calls, sites, depth = calls_map["calls"], {}, [0]
    import hashlib

    for name in HELPERS:
        orig = getattr(Validator, name)

        def wrapper(self, *args, _orig=orig, **kwargs):
            test = run_tests.CURRENT["test"]
            if depth[0] or test is None:
                depth[0] += 1
                try:
                    return _orig(self, *args, **kwargs)
                finally:
                    depth[0] -= 1
            frame = sys._getframe(1)
            rel = os.path.relpath(frame.f_code.co_filename, repo)
            site = "%s:%d" % (rel, frame.f_lineno)
            h = hashlib.blake2b(repr((args, sorted(kwargs.items()))).encode(), digest_size=10).hexdigest()
            counts = run_tests.CURRENT["counts"]
            n = counts.get((site, h), 0)
            counts[(site, h)] = n + 1
            entry = calls.get((test, site, h, n))
            bare = any(lo <= frame.f_lineno <= hi for lo, hi in _bare_sites(repo, rel, sites))
            if entry is not None and bare and not changed.intersection(entry[0]):
                STATS["skipped"] += 1
                return None  # this call cannot see the patch: it passes as before
            STATS["ran" if entry is not None else "unknown"] += 1
            depth[0] += 1
            try:
                return _orig(self, *args, **kwargs)
            finally:
                depth[0] -= 1
        setattr(Validator, name, wrapper)
    return True


def main():
    repo = os.path.abspath(sys.argv[1])
    sys.path.insert(0, repo)
    sys.path.insert(0, HERE)
    os.chdir(repo)
    import import_state
    import run_tests
    import_state.import_all(repo)
    run_tests._threads_for_pools()
    calls_map = None
    if os.path.exists(CALLS):
        import zlib
        with open(CALLS, "rb") as f:
            calls_map = pickle.loads(zlib.decompress(f.read()))
            calls_map["calls"] = {k: (frozenset(v[0]), v[1]) for k, v in calls_map["calls"].items()}
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        for tid in req["tests"]:  # imported once here, shared by every later child
            try:
                importlib.import_module(tid.split("::", 1)[0][:-3].replace("/", "."))
            except BaseException:
                pass
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                why = swap(req["files"], req["functions"])
                if not why and calls_map is not None and req.get("skip_calls"):
                    skip_unaffected_calls(repo, calls_map, req["functions"])
                out = {"cannot": why} if why else run_tests.run_here(
                    req["tests"], req.get("seconds") or {}, req.get("first") or ())
                out["calls"] = dict(STATS)
            except BaseException as error:
                out = {"cannot": repr(error)[:200]}
            with os.fdopen(w, "w") as f:
                f.write(json.dumps(out))
            os._exit(0)
        os.close(w)
        with os.fdopen(r) as f:
            text = f.read()
        _, _, usage = os.wait4(pid, 0)
        try:
            answer = json.loads(text)
            answer["cpu"] = round(usage.ru_utime + usage.ru_stime, 3)
            text = json.dumps(answer)
        except ValueError:
            pass
        sys.stdout.write((text or json.dumps({"cannot": "child died"})) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

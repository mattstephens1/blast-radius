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


def main():
    repo = os.path.abspath(sys.argv[1])
    sys.path.insert(0, repo)
    sys.path.insert(0, HERE)
    os.chdir(repo)
    import import_state
    import run_tests
    import_state.import_all(repo)
    run_tests._threads_for_pools()
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
                out = {"cannot": why} if why else run_tests.run_here(
                    req["tests"], req.get("seconds") or {}, req.get("first") or ())
            except BaseException as error:
                out = {"cannot": repr(error)[:200]}
            with os.fdopen(w, "w") as f:
                f.write(json.dumps(out))
            os._exit(0)
        os.close(w)
        with os.fdopen(r) as f:
            text = f.read()
        os.waitpid(pid, 0)
        sys.stdout.write((text or json.dumps({"cannot": "child died"})) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

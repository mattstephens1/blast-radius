"""One process per commit that imports the patched sqlglot once and answers the
selector's checks, instead of one fresh process (and import) per check.

    python patched_worker.py <repo>
    stdin:  first line {"overrides": {path: source}}, then one request per line
    stdout: one JSON answer per request

Requests:
    {"op": "import_diff", "baseline": path}        -> import_state.check
    {"op": "max_nodes"}                            -> maxnodes_check.evaluate
    {"op": "tpc", "fixtures", "baseline", "suites", "only"} -> tpc_check.check

The import-state snapshot must see sqlglot exactly as imported, before any check
has run code that fills caches (the generator's dispatch cache is a module
global). So it is only answered as the first request; asked later, the answer
is "unknown" (ok: false), which the selector treats as it would a failed check.
"""
# sqlglot turns on extra validation when it is imported under a test runner
# (expressions/core.py: UNITTEST = "unittest" in sys.modules or "pytest" in
# sys.modules). Import unittest first so every check sees what the tests see.
import unittest  # noqa: F401
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    repo = os.path.abspath(sys.argv[1])
    sys.path.insert(0, repo)
    sys.path.insert(0, HERE)
    import import_state
    first = json.loads(sys.stdin.readline())
    overrides = first.get("overrides") or {}
    if overrides:
        sys.meta_path.insert(0, import_state._Patched(repo, overrides))
    served = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        op = req.get("op")
        try:
            if op == "import_diff":
                if served:
                    out = {"ok": False, "error": "import state asked after other checks ran"}
                else:
                    out = import_state.check(repo, None, req["baseline"])
            elif op == "max_nodes":
                import maxnodes_check
                try:
                    passes, why = maxnodes_check.evaluate()
                    out = {"ok": True, "passes": passes, "why": why}
                except BaseException as error:  # the test would error out too
                    out = {"ok": True, "passes": False, "why": repr(error)[:200]}
            elif op == "tpc":
                import tpc_check
                only = set(req["only"]) if req.get("only") is not None else None
                out = tpc_check.check(req["fixtures"], req["baseline"], req["suites"], only,
                                      req.get("execute") or ())
            else:
                out = {"ok": False, "error": "unknown op %r" % op}
        except BaseException as error:
            out = {"ok": False, "error": repr(error)[:300]}
        served += 1
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

"""Run a few of sqlglot's tests against the patched source, inside the container.

When a commit selects only a handful of quick tests, running them here costs
less CPU than the 2 s a worker costs on the simulated clock. Tests that pass
here can be left out; tests that fail must be run. Anything that cannot be
judged here (its module needs DuckDB or pandas, it was skipped, its class
setup broke, or the run timed out) is reported as unknown and stays selected.

    python run_tests.py <repo>   (stdin: {"overrides": {path: source}, "tests": [test ids]})
    stdout: {"passed": [...], "failed": [...], "unknown": [...]}
"""
import importlib
import io
import json
import os
import sys
import unittest


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad, self.skip, self.ran = set(), set(), set()

    @staticmethod
    def _id(test):
        test = getattr(test, "test_case", test)  # a subtest reports through its parent
        return test.id()

    def startTest(self, test):
        super().startTest(test)
        self.ran.add(self._id(test))

    def addError(self, test, err):
        super().addError(test, err)
        self.bad.add(self._id(test))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.bad.add(self._id(test))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.bad.add(self._id(test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.skip.add(self._id(test))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.skip.add(self._id(test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.bad.add(self._id(test))


def run(repo, overrides, test_ids):
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if overrides:
        import import_state
        sys.meta_path.insert(0, import_state._Patched(repo, overrides))
    os.chdir(repo)
    by_dotted, suite, unknown = {}, unittest.TestSuite(), set()
    for tid in test_ids:
        path, cls, meth = tid.split("::")
        module = path[:-3].replace("/", ".")
        try:
            case = getattr(importlib.import_module(module), cls)(meth)
        except BaseException:
            unknown.add(tid)
            continue
        by_dotted[case.id()] = tid
        suite.addTest(case)
    result = _Result()
    # unittest calls setUpClass once per class; a failure there is reported as an
    # error on a "_ErrorHolder" whose id names the class, not a test
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        suite.run(result)
    finally:
        sys.stderr = stderr
    passed, failed = [], []
    holders = {t.id() for t, _ in result.errors if t.id() not in by_dotted}
    for dotted, tid in by_dotted.items():
        cls_id = dotted.rsplit(".", 1)[0]
        if dotted in result.bad:
            failed.append(tid)
        elif dotted in result.skip or dotted not in result.ran or any(cls_id in h for h in holders):
            unknown.add(tid)
        else:
            passed.append(tid)
    return {"passed": passed, "failed": failed, "unknown": sorted(unknown)}


def main():
    request = json.load(sys.stdin)
    out = run(os.path.abspath(sys.argv[1]), request.get("overrides") or {}, request["tests"])
    sys.stdout.write("\n" + json.dumps(out) + "\n")


if __name__ == "__main__":
    main()

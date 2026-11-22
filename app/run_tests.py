"""Run a few of sqlglot's tests against the patched source, inside the container.

The brief allows it (the CPU is charged). When a commit selects only a few quick
tests, running them costs less than the 2 s a worker costs on the simulated
clock, and a commit whose tests all pass needs no worker at all.

Every judgement errs towards keeping a test:
  passed   ran to the end here, no failure or error in it or any subtest
  failed   failed or errored here: the commit fails, so the run stops there
  unknown  everything else: not run, skipped, its module would not import, its
           class setup broke, it took more than its time limit (a timeout fails
           a test in the scoring), or it is one of the tests that start Python
           subprocesses, which would run the unpatched source

Process pools (test_optimizer, test_executor) would run the tests' work in
fresh processes importing the unpatched source from disk; they become threads
that still pickle arguments and results.

    in a process whose imports already see the patched source (patched_worker.py):
        run_here(test_ids, recorded_seconds) -> {"passed", "failed", "unknown"}
"""
# sqlglot turns on extra validation when it is imported under a test runner
# (expressions/core.py: UNITTEST = "unittest" in sys.modules or "pytest" in
# sys.modules). Import unittest first so every check sees what the tests see.
import unittest  # noqa: F401
import importlib
import io
import os
import pickle
import signal
import sys

# tests that start Python subprocesses: those would import the unpatched source
SUBPROCESS_FILES = ("tests/test_sqlglotc.py", "tests/test_build.py")
SUBPROCESS_TESTS = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)


def runnable(test_id):
    return test_id.split("::", 1)[0] not in SUBPROCESS_FILES and test_id not in SUBPROCESS_TESTS


def _threads_for_pools():
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    import multiprocessing as mp
    import multiprocessing.pool as mpp
    if getattr(cf.ProcessPoolExecutor, "_in_process", False):
        return

    class InProc(cf.ThreadPoolExecutor):
        _in_process = True

        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            if initializer is not None:  # what a worker process would get: pickled copies
                initargs = pickle.loads(pickle.dumps(initargs))
            super().__init__(max_workers=max_workers, initializer=initializer, initargs=initargs)

        def submit(self, fn, *args, **kwargs):
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))

            def run():
                return pickle.loads(pickle.dumps(fn(*args, **kwargs)))

            return super().submit(run)

    cf.ProcessPoolExecutor = InProc
    cfp.ProcessPoolExecutor = InProc
    mp.Pool = mpp.ThreadPool


CURRENT = {"test": None, "counts": None}  # the test being run (for call-level skipping)


class _TimeLimit(Exception):
    pass


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad, self.skip, self.ran, self.timed_out = set(), set(), set(), set()

    @staticmethod
    def _id(test):
        return getattr(test, "test_case", test).id()  # a subtest reports through its parent

    def _err(self, test, err):
        if err is not None and err[0] is _TimeLimit:
            self.timed_out.add(self._id(test))
        else:
            self.bad.add(self._id(test))
            self.stop()  # one failure fails the commit: nothing more to learn

    limits = {}
    test_ids = {}

    def startTest(self, test):
        super().startTest(test)
        self.ran.add(self._id(test))
        CURRENT["test"], CURRENT["counts"] = self.test_ids.get(self._id(test)), {}
        signal.setitimer(signal.ITIMER_REAL, self.limits.get(self._id(test), 5.0))

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        CURRENT["test"], CURRENT["counts"] = None, None
        super().stopTest(test)

    def addError(self, test, err):
        super().addError(test, err)
        self._err(test, err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._err(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._err(test, err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.skip.add(self._id(test))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.skip.add(self._id(test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.bad.add(self._id(test))
        self.stop()


def run_here(test_ids, seconds, first=()):
    """Run these tests in this process; see the module doc. Tests whose file names
    contain one of `first` (the changed modules' names, likeliest to fail) run
    first, then the fastest: the run stops at the first failure."""
    _threads_for_pools()
    unknown = {t for t in test_ids if not runnable(t)}
    cases = []
    for tid in sorted((t for t in test_ids if runnable(t)), key=lambda t: seconds.get(t, 0)):
        path, cls, meth = tid.split("::")
        try:
            case = getattr(importlib.import_module(path[:-3].replace("/", ".")), cls)(meth)
        except BaseException:
            unknown.add(tid)
            continue
        cases.append((tid, case))
    result = _Result()

    def alarm(signum, frame):
        raise _TimeLimit()

    result.limits = {case.id(): max(5.0, 5 * seconds.get(tid, 0)) for tid, case in cases}
    result.test_ids = {case.id(): tid for tid, case in cases}
    # one suite per class (so setUpClass runs once), cheapest classes first
    classes = {}
    for tid, case in cases:
        classes.setdefault(type(case), []).append((tid, case))
    def likely(group):
        name = group[0][0].split("::", 1)[0].rsplit("/", 1)[-1]
        return not any(word in name for word in first)

    order = sorted(classes.values(), key=lambda group: (likely(group), sum(seconds.get(t, 0) for t, _ in group)))
    old = signal.signal(signal.SIGALRM, alarm)
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        for group in order:
            if result.shouldStop:
                break
            # class setup gets its own limit (startTest re-arms the timer per test)
            signal.setitimer(signal.ITIMER_REAL, 60.0)
            try:
                unittest.TestSuite([case for _, case in group]).run(result)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.signal(signal.SIGALRM, old)
        sys.stderr = stderr
    by_dotted = {case.id(): tid for tid, case in cases}
    holders = {t.id() for t, _ in result.errors if t.id() not in by_dotted}  # setUpClass errors
    passed, failed = [], []
    for dotted, tid in by_dotted.items():
        cls_id = dotted.rsplit(".", 1)[0]
        if dotted in result.bad:
            failed.append(tid)
        elif (dotted in result.skip or dotted in result.timed_out or dotted not in result.ran
              or any(cls_id in h for h in holders)):
            unknown.add(tid)
        else:
            passed.append(tid)
    return {"passed": passed, "failed": failed, "unknown": sorted(unknown)}

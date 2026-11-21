"""pytest plugin used once, offline, to build the coverage map.

Run from a sqlglot checkout at the pinned commit:

    PYTHONPATH=path/to/tools python -m pytest -p coverage_plugin tests

Records, per test and per phase (setup / call / teardown), which sqlglot lines
ran, plus which files under tests/ each test opened. Lines that run while no
test is active (imports, collection) are recorded under the empty context.
Process pools are swapped for threads that still pickle arguments and results,
because coverage cannot see inside worker processes.
"""
import concurrent.futures as _cf
import concurrent.futures.process as _cfp
import json
import os
import pickle
import sys

import coverage
import pytest

OUT_DIR = os.environ.get("MAP_OUT", "/tmp/map")
os.makedirs(OUT_DIR, exist_ok=True)


class _InProc(_cf.ThreadPoolExecutor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        super().__init__(max_workers=max_workers, initializer=initializer, initargs=initargs)

    def submit(self, fn, *args, **kwargs):
        fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))

        def run():
            return pickle.loads(pickle.dumps(fn(*args, **kwargs)))

        return super().submit(run)


_cf.ProcessPoolExecutor = _InProc
_cfp.ProcessPoolExecutor = _InProc

ROOT = os.path.abspath(".")
cov = coverage.Coverage(data_file=os.path.join(OUT_DIR, ".cov"), source=["sqlglot"])
OPENED = {}
_cur = [None]
_phase = [""]
OPENS_ONLY = os.environ.get("OPENS_ONLY") == "1"  # just re-record file opens, no coverage


def _hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        path = os.path.abspath(os.fsdecode(args[0]))
        if path.startswith(ROOT + "/tests/"):
            key = (_cur[0] + "|" + _phase[0]) if _cur[0] else "<no test>"
            OPENED.setdefault(key, set()).add(os.path.relpath(path, ROOT))


sys.addaudithook(_hook)


def pytest_sessionstart(session):
    if not OPENS_ONLY:
        cov.start()


def _switch(item, phase):
    _cur[0] = item.nodeid
    _phase[0] = phase
    if not OPENS_ONLY:
        cov.switch_context(f"{item.nodeid}|{phase}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _switch(item, "setup")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _switch(item, "call")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    _switch(item, "teardown")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    yield
    _cur[0] = None
    if not OPENS_ONLY:
        cov.switch_context("")


def pytest_sessionfinish(session):
    if not OPENS_ONLY:
        cov.stop()
        cov.save()
    with open(os.path.join(OUT_DIR, "opened.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in OPENED.items()}, f)

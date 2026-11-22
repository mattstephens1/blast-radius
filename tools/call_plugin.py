"""pytest plugin used once, offline, to build maps/calls.pkl.z.

For every call of a Validator assertion helper (validate_identity, validate_all,
validate_transpile) made by sqlglot's dialect tests, records which sqlglot
functions the call executes. A call that executes none of the functions a patch
changes (when the patch changes nothing but function bodies) behaves exactly as
before, so app/test_server.py can skip it.

    CALLS_OUT=/out PYTHONPATH=/tools python -m pytest -p call_plugin -p no:cacheprovider tests/dialects

A call is identified by (test node id, call site line, hash of its arguments,
how many identical calls came before it in that test). Also records the
functions run while the generator builds a dispatch table (lazily, once per
generator class, inside whichever call first needs it): a patch to one of
those makes skipping unsafe.
"""
import hashlib
import os
import pickle
import sys
import time
import zlib

import pytest

OUT = os.environ.get("CALLS_OUT", "/tmp/calls")
os.makedirs(OUT, exist_ok=True)
HELPERS = ("validate_identity", "validate_all", "validate_transpile")
ROOT = os.path.abspath(".")

_names, _ids = [], {}          # function name table
_calls = {}                    # key -> frozenset of function ids
_lazy = set()                  # function ids run inside _build_dispatch
_state = {"test": None, "depth": 0, "seen": None, "counts": None, "lazy": 0}


def _fid(code):
    key = "%s:%s" % (os.path.relpath(code.co_filename, ROOT), code.co_qualname.replace(".<locals>", ""))
    i = _ids.get(key)
    if i is None:
        i = _ids[key] = len(_names)
        _names.append(key)
    return i


def _prof(frame, event, arg):
    if event != "call":
        return
    co = frame.f_code
    if not co.co_filename.startswith(ROOT + "/sqlglot/"):
        return
    if _state["seen"] is not None:
        _state["seen"].add(_fid(co))
    if _state["lazy"]:
        _lazy.add(_fid(co))


def call_key(test, frame, args, kwargs, counts):
    site = "%s:%d" % (os.path.relpath(frame.f_code.co_filename, ROOT), frame.f_lineno)
    h = hashlib.blake2b(repr((args, sorted(kwargs.items()))).encode(), digest_size=10).hexdigest()
    n = counts.get((site, h), 0)
    counts[(site, h)] = n + 1
    return (test, site, h, n)


def _install():
    sys.path.insert(0, ROOT)
    from tests.dialects.test_dialect import Validator
    import sqlglot.generator as g

    for name in HELPERS:
        orig = getattr(Validator, name)

        def wrapper(self, *args, _orig=orig, _name=name, **kwargs):
            if _state["depth"] or _state["test"] is None:  # nested helper calls belong to the outer one
                _state["depth"] += 1
                try:
                    return _orig(self, *args, **kwargs)
                finally:
                    _state["depth"] -= 1
            key = call_key(_state["test"], sys._getframe(1), args, kwargs, _state["counts"])
            outer, _state["seen"] = _state["seen"], set()
            _state["depth"] += 1
            t = time.process_time()
            try:
                return _orig(self, *args, **kwargs)
            finally:
                _state["depth"] -= 1
                _calls[key] = (frozenset(_state["seen"]), time.process_time() - t)
                _state["seen"] = outer
        setattr(Validator, name, wrapper)

    orig_build = g._build_dispatch

    def build(cls):
        _state["lazy"] += 1
        try:
            return orig_build(cls)
        finally:
            _state["lazy"] -= 1
    g._build_dispatch = build
    sys.setprofile(_prof)


def pytest_sessionstart(session):
    _install()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _state["test"], _state["counts"] = item.nodeid, {}
    yield
    _state["test"], _state["counts"] = None, None


def pytest_sessionfinish(session):
    sys.setprofile(None)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(OUT, "calls-%s.pkl.z" % worker), "wb") as f:
        f.write(zlib.compress(pickle.dumps({"names": _names, "calls": _calls, "lazy": _lazy}, protocol=5), 9))

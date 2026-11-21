"""pytest plugin used once, offline, to build maps/reads.pkl.z.

Run from a sqlglot checkout at the pinned commit (the labeler image has one):

    READS_OUT=/out PYTHONPATH=/tools:/app python -m pytest -p read_plugin -p no:cacheprovider tests

Records, per test, which items of sqlglot's import-time containers it reads at
run time. Every dict, set and frozenset held by a sqlglot module global or
class attribute (the values app/import_state.py fingerprints) is swapped for
a subclass that logs reads:

* d[k], d.get(k), k in d, s contains k  -> (attribute key, item k)
* anything that touches the whole container (iterate, len, copy, compare,
  set algebra, repr)                       -> (attribute key, "<all>")

so a recorded set of reads can only overstate what a test depends on.
The generator's dispatch cache (built once per generator class from
TRANSFORMS, then reused by later tests) is wrapped too, so a cached lookup
still counts as a read of the TRANSFORMS entry it came from.

Reads outside any test (collection) are kept under "<no test>". Tests that
fail only under this instrumentation are listed, so their reads can be
treated as unknown.
"""
import gc
import os
import pickle
import sys
import zlib

import pytest

# Process pools (test_optimizer, test_executor) would run tests' work in fresh
# processes this plugin cannot see into: swap them for threads that still
# pickle arguments and results, as tools/coverage_plugin.py does.
import concurrent.futures as _cf
import concurrent.futures.process as _cfp
import multiprocessing as _mp
import multiprocessing.pool as _mpp


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
_mp.Pool = _mpp.ThreadPool  # in case a test uses multiprocessing.Pool

OUT = os.environ.get("READS_OUT", "/tmp/reads")
os.makedirs(OUT, exist_ok=True)
ALL = object()     # "the whole container was read"; written out as ALL_KEY
ALL_KEY = "<all>"  # no item key looks like this (strings are repr()'d)

_ctx = [None]          # current test node id + phase
_reads = {}            # context -> set of (label id, item)
_labels = []           # label id -> list of attribute keys ("module:Class.ATTR")
_outcomes = {}


def _collecting_module():
    """The test module being imported when a read happens outside any test."""
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        i = fn.find("/tests/")
        if i >= 0 and f.f_code.co_name == "<module>":
            return "<collect:%s>" % fn[i + 1:]
        f = f.f_back
    return "<no test>"


def _rec(label, item):
    ctx = _ctx[0] or _collecting_module()
    s = _reads.get(ctx)
    if s is None:
        s = _reads[ctx] = set()
    try:
        s.add((label, item))
    except TypeError:  # unhashable probe, e.g. a list tested with `in`
        s.add((label, ALL))


def _make(base):
    def whole(name):
        orig = getattr(base, name)

        def f(self, *a, **k):
            _rec(self._label, ALL)
            return orig(self, *a, **k)
        f.__name__ = name
        return f

    ns = {"__slots__": ("_label",)}
    if base is dict:
        def __getitem__(self, k):
            _rec(self._label, k)
            return dict.__getitem__(self, k)

        def get(self, k, default=None):
            _rec(self._label, k)
            return dict.get(self, k, default)

        def __contains__(self, k):
            _rec(self._label, k)
            return dict.__contains__(self, k)

        ns.update(__getitem__=__getitem__, get=get, __contains__=__contains__)
        for n in ("__iter__", "keys", "values", "items", "__len__", "copy", "__eq__", "__ne__",
                  "__or__", "__ror__", "__repr__", "__reversed__", "setdefault", "pop", "popitem",
                  "update", "__setitem__", "__delitem__", "clear", "__ior__", "__reduce_ex__"):
            ns[n] = whole(n)
    else:
        def __contains__(self, k):
            _rec(self._label, k)
            return base.__contains__(self, k)

        ns["__contains__"] = __contains__
        names = ["__iter__", "__len__", "copy", "__eq__", "__ne__", "__le__", "__lt__", "__ge__",
                 "__gt__", "__or__", "__ror__", "__and__", "__rand__", "__sub__", "__rsub__",
                 "__xor__", "__rxor__", "union", "intersection", "difference",
                 "symmetric_difference", "issubset", "issuperset", "isdisjoint", "__repr__",
                 "__reduce_ex__"]
        if base is set:
            names += ["add", "discard", "remove", "pop", "clear", "update", "__ior__", "__iand__",
                      "__isub__", "__ixor__", "difference_update", "intersection_update",
                      "symmetric_difference_update"]
        for n in names:
            if hasattr(base, n):
                ns[n] = whole(n)
        if base is frozenset:
            ns["__hash__"] = frozenset.__hash__
    return type("Rec" + base.__name__, (base,), ns)


RecDict, RecSet, RecFrozenSet = _make(dict), _make(set), _make(frozenset)


class RecAttr:
    """Stands in for a plain class attribute (a flag, number, string, tuple...):
    returns the same value, and logs that it was read, on instance and class access."""
    __slots__ = ("value", "label")

    def __init__(self, value, label):
        self.value, self.label = value, label

    def __get__(self, obj, objtype=None):
        _rec(self.label, ALL)
        return self.value


def _plain(value):
    """A class attribute worth tracking as a scalar."""
    import enum
    import types
    if type(value) in _WRAP or isinstance(value, type) or callable(value):
        return False
    if isinstance(value, (staticmethod, classmethod, property, types.MemberDescriptorType,
                          types.GetSetDescriptorType, enum.Enum)) or hasattr(type(value), "__get__"):
        return False
    return True
_WRAP = {dict: RecDict, set: RecSet, frozenset: RecFrozenSet}


def _wrap(value, label):
    cls = _WRAP[type(value)]
    w = cls.__new__(cls, value) if cls is RecFrozenSet else cls()
    if cls is not RecFrozenSet:
        type(value).update(w, value) if cls is RecDict else set.update(w, value)
    w._label = label
    return w


def _install(repo):
    sys.path.insert(0, repo)
    sys.path.insert(0, os.environ.get("APP_DIR", "/app"))
    import import_state
    import_state.import_all(repo)
    owners = []  # (owner object, attr, key)
    for name in sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")):
        mod = sys.modules[name]
        for attr, value in list(vars(mod).items()):
            if attr.startswith("__"):  # __builtins__, __spec__ ...: not sqlglot's values
                continue
            owners.append((mod, attr, "%s:%s" % (name, attr), value))
            if isinstance(value, type) and value.__module__ == name and value.__qualname__ == value.__name__:
                stack = [(value, value.__qualname__)]
                while stack:
                    cls, q = stack.pop()
                    for a, v in list(vars(cls).items()):
                        if a.startswith("__"):
                            continue
                        owners.append((cls, a, "%s:%s.%s" % (name, q, a), v))
                        if isinstance(v, type) and v.__qualname__ == "%s.%s" % (q, a):
                            stack.append((v, v.__qualname__))
    by_id = {}
    for owner, attr, key, value in owners:
        if type(value) not in _WRAP:
            continue
        if id(value) not in by_id:
            label = len(_labels)
            _labels.append([])
            by_id[id(value)] = (value, _wrap(value, label))
        _labels[by_id[id(value)][1]._label].append(key)
    # every reference to an original container now points at its wrapper
    replaced, tuples = 0, 0
    originals = {i: v for i, (v, _) in by_id.items()}
    for i, (orig, w) in by_id.items():
        for ref in gc.get_referrers(orig):
            if ref is originals or ref is by_id:
                continue
            if isinstance(ref, dict):
                for k, v in list(ref.items()):
                    if v is orig:
                        try:
                            ref[k] = w
                            replaced += 1
                        except TypeError:  # a class's mappingproxy-backed dict: use setattr
                            pass
            elif isinstance(ref, list):
                for j, v in enumerate(ref):
                    if v is orig:
                        ref[j] = w
                        replaced += 1
            elif isinstance(ref, tuple):
                tuples += 1
    for owner, attr, key, value in owners:
        if id(value) in by_id:
            try:
                setattr(owner, attr, by_id[id(value)][1])
            except (AttributeError, TypeError):
                pass
    # plain class attributes: swap in a recording descriptor (not for enums, whose
    # members are their class attributes, nor where a metaclass forbids setattr)
    import enum
    scalars = 0
    for owner, attr, key, value in owners:
        if not isinstance(owner, type) or issubclass(owner, enum.Enum) or not _plain(value):
            continue
        if owner.__dict__.get(attr) is not value:
            continue
        label = len(_labels)
        _labels.append([key])
        try:
            setattr(owner, attr, RecAttr(value, label))
            scalars += 1
        except (AttributeError, TypeError):
            pass
    print("read_plugin: %d plain class attributes tracked" % scalars, file=sys.stderr)
    _wrap_dispatch()
    print("read_plugin: %d containers, %d references replaced, %d held in tuples"
          % (len(by_id), replaced, tuples), file=sys.stderr)
    global _sizes
    _sizes = {w._label: len(dict.keys(w)) if isinstance(w, dict) else set.__len__(w) if isinstance(w, set)
              else frozenset.__len__(w) for _, w in by_id.values()}
    global _wrappers
    _wrappers = [w for _, w in by_id.values()]


def _wrap_dispatch():
    import sqlglot.generator as g
    orig = g._build_dispatch

    def build(cls):
        d = orig(cls)
        owner = next((c for c in cls.__mro__ if "TRANSFORMS" in c.__dict__), None)
        t = owner.__dict__["TRANSFORMS"] if owner else None
        label = getattr(t, "_label", None)
        if label is None:
            return d
        w = RecDict()
        dict.update(w, d)
        w._label = label  # a dispatch lookup is a read of that TRANSFORMS entry
        return w

    g._build_dispatch = build


_sizes, _wrappers = {}, []


def pytest_sessionstart(session):
    _install(os.path.abspath("."))


def _switch(item, phase):
    _ctx[0] = "%s|%s" % (item.nodeid, phase)


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
    _ctx[0] = None


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"


def pytest_sessionfinish(session):
    grew = []
    for w in _wrappers:
        n = len(dict.keys(w)) if isinstance(w, dict) else set.__len__(w) if isinstance(w, set) \
            else frozenset.__len__(w)
        if n != _sizes.get(w._label):
            grew.append((_labels[w._label], _sizes.get(w._label), n))
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(OUT, "reads-%s.pkl.z" % worker), "wb") as f:
        f.write(zlib.compress(pickle.dumps({
            "labels": _labels, "reads": {k: {(l, _item_key(i)) for l, i in v} for k, v in _reads.items()},
            "failed": _outcomes, "grew": grew}), 6))


def _item_key(item):
    """Items as the strings import_state's item-level diff uses."""
    if item is ALL:
        return ALL_KEY
    import import_state
    return import_state.item_key(item)

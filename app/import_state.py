"""Import-time state of sqlglot, for telling whether a patch changes it.

Some changed functions also run while sqlglot is being imported: the parser,
for instance, builds SQL templates in generators/duckdb.py at import time. A
change to such a function can reach tests in two ways: tests that call it
(the coverage map finds those) and tests that use whatever it built during
import. This module checks the second way directly: import every sqlglot
module from the patched source in a fresh process, take a canonical
fingerprint of every module global and class attribute, and compare it with
the same fingerprint of the unpatched source, taken once at image build time.

If nothing differs, the import-time state is unchanged and only tests that run
the changed code can notice the patch. If something differs, or the patched
import fails, the caller falls back to its broad rules.

    python import_state.py --baseline <repo> <out.json>   (image build)
    python import_state.py --check <repo> <baseline.json> (stdin: {path: source})
"""
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import pkgutil
import re
import sys
import types

PACKAGES = ("dialects", "parsers", "generators", "tokenizers", "typing", "expressions",
            "optimizer", "executor")
_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
_FUNCS = (types.FunctionType, types.BuiltinFunctionType, types.MethodType,
          types.BuiltinMethodType, types.WrapperDescriptorType, types.MethodDescriptorType,
          staticmethod, classmethod)


class _Patched(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serve patched source for some files of the package, never stale bytecode."""

    def __init__(self, repo, overrides):
        self.repo, self.by_mod = repo, {}
        for rel, src in overrides.items():
            parts = rel[:-3].split("/")
            pkg = parts[-1] == "__init__"
            name = ".".join(parts[:-1] if pkg else parts)
            self.by_mod[name] = (os.path.join(repo, rel), src, pkg)

    def find_spec(self, name, path=None, target=None):
        if name not in self.by_mod:
            return None
        file, _, pkg = self.by_mod[name]
        return importlib.util.spec_from_file_location(
            name, file, loader=self,
            submodule_search_locations=[os.path.dirname(file)] if pkg else None)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        file, src, _ = self.by_mod[module.__name__]
        exec(compile(src, file, "exec", dont_inherit=True), module.__dict__)


def import_all(repo, overrides=None):
    sys.path.insert(0, repo)
    if overrides:
        sys.meta_path.insert(0, _Patched(repo, overrides))
    import sqlglot  # noqa: F401
    for pkg in PACKAGES:
        if not os.path.isdir(os.path.join(repo, "sqlglot", pkg)):
            continue
        mod = importlib.import_module("sqlglot." + pkg)
        for info in pkgutil.walk_packages(mod.__path__, "sqlglot.%s." % pkg):
            importlib.import_module(info.name)
    for info in pkgutil.iter_modules([os.path.join(repo, "sqlglot")]):
        if info.name != "__main__":
            importlib.import_module("sqlglot." + info.name)


def _ours(obj):
    return (getattr(obj, "__module__", None) or "").startswith("sqlglot")


class _Canon:
    def __init__(self):
        self.memo = {}

    def __call__(self, obj, depth=0):
        key = id(obj)
        if key in self.memo:
            return self.memo[key]
        if depth > 40:
            return "<deep>"
        self.memo[key] = "<cycle>"
        out = self._canon(obj, depth + 1)
        self.memo[key] = out
        return out

    def _canon(self, obj, d):
        c = self
        if obj is None or isinstance(obj, (bool, int, float, complex, str, bytes)):
            return repr(obj)
        if isinstance(obj, type):
            return "<type %s.%s>" % (obj.__module__, obj.__qualname__)
        if isinstance(obj, types.ModuleType):
            return "<module %s>" % obj.__name__
        if isinstance(obj, (staticmethod, classmethod)):
            return "%s(%s)" % (type(obj).__name__, c(obj.__func__, d))
        if isinstance(obj, types.FunctionType):
            # the code itself is the coverage map's business; what was captured
            # or defaulted at import time is ours
            cells = []
            for cell in obj.__closure__ or ():
                try:
                    cells.append(c(cell.cell_contents, d))
                except ValueError:
                    cells.append("<empty>")
            return "<fn %s.%s d=%s k=%s c=[%s]>" % (
                obj.__module__, obj.__qualname__, c(obj.__defaults__, d),
                c(obj.__kwdefaults__, d), ",".join(cells))
        if isinstance(obj, _FUNCS):
            return "<fn %s.%s>" % (getattr(obj, "__module__", ""), getattr(obj, "__qualname__", repr(obj)))
        if isinstance(obj, (list, tuple)):
            items = [c(x, d) for x in obj]
            if obj and all(isinstance(x, type) for x in obj):
                items.sort()  # often built from a set of classes, whose order follows id()
            return "%s[%s]" % (type(obj).__name__, ",".join(items))
        if isinstance(obj, (set, frozenset)):
            return "%s{%s}" % (type(obj).__name__, ",".join(sorted(c(x, d) for x in obj)))
        if isinstance(obj, dict):
            # sorted: dicts keyed by classes are often filled from sets, in id() order
            return "%s{%s}" % (type(obj).__name__,
                               ",".join(sorted("%s:%s" % (c(k, d), c(v, d)) for k, v in obj.items())))
        if isinstance(obj, re.Pattern):
            return "<re %r %d>" % (obj.pattern, obj.flags)
        if isinstance(obj, property):
            return "<property %s %s %s>" % (c(obj.fget, d), c(obj.fset, d), c(obj.fdel, d))
        if hasattr(obj, "func") and hasattr(obj, "args") and hasattr(obj, "keywords"):  # partial
            return "<partial %s %s %s>" % (c(obj.func, d), c(obj.args, d), c(obj.keywords, d))
        tname = "%s.%s" % (type(obj).__module__, type(obj).__qualname__)
        if type(obj).__module__.startswith("sqlglot"):
            if hasattr(obj, "args") and hasattr(obj, "arg_types"):  # an expression
                return "<%s %s %s>" % (tname, repr(obj), c(getattr(obj, "meta", None), d))
            state = {}
            if hasattr(obj, "__dict__"):
                state.update(vars(obj))
            for cls in type(obj).__mro__:
                for slot in getattr(cls, "__slots__", ()):
                    if isinstance(slot, str) and hasattr(obj, slot):
                        state.setdefault(slot, getattr(obj, slot))
            if hasattr(obj, "name") and hasattr(obj, "value") and not state:  # enum member
                return "<%s.%s>" % (tname, obj.name)
            return "<%s %s>" % (tname, c(state, d))
        try:
            return "<%s %s>" % (tname, _ADDR.sub("", repr(obj)))
        except Exception:
            return "<%s>" % tname


def snapshot():
    """key (module:attr or module:Class.attr) -> hash of its canonical form."""
    canon, out = _Canon(), {}

    def put(key, value):
        out[key] = hashlib.blake2b(canon(value).encode(), digest_size=12).hexdigest()

    def walk_class(prefix, cls, seen):
        if cls in seen:
            return
        seen.add(cls)
        put(prefix + ".__bases__", cls.__bases__)
        put(prefix + ".__metaclass__", type(cls))
        for attr, value in list(vars(cls).items()):
            if attr in ("__dict__", "__weakref__", "__doc__", "__annotations__",
                        "__annotate__", "__module__", "__qualname__", "__firstlineno__",
                        "__static_attributes__"):
                continue
            if isinstance(value, type) and _ours(value) and value.__qualname__.startswith(cls.__qualname__ + "."):
                walk_class(prefix + "." + attr, value, seen)
            else:
                put(prefix + "." + attr, value)

    seen = set()
    for name in sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")):
        mod = sys.modules[name]
        for attr, value in list(vars(mod).items()):
            if attr in ("__builtins__", "__loader__", "__spec__", "__file__", "__cached__",
                        "__path__", "__doc__", "__annotations__", "__annotate__"):
                continue
            key = "%s:%s" % (name, attr)
            if isinstance(value, types.ModuleType):
                continue
            if isinstance(value, type) and _ours(value):
                if value.__module__ == name and value.__qualname__ == value.__name__:
                    walk_class(key, value, seen)
                else:
                    put(key, value)  # a class imported from elsewhere: just its identity
            elif isinstance(value, (types.FunctionType, type)) and value.__module__ != name:
                put(key, value)  # imported function: identity only, walked in its home module
            else:
                put(key, value)
    return out


def main():
    mode, repo, path = sys.argv[1:4]
    if mode == "--baseline":
        import_all(repo)
        with open(path, "w") as f:
            json.dump(snapshot(), f)
        return
    overrides = json.load(sys.stdin)
    try:
        import_all(repo, overrides)
        now = snapshot()
    except BaseException as error:  # the patched package does not even import
        print(json.dumps({"ok": False, "error": repr(error)[:300]}))
        return
    with open(path) as f:
        base = json.load(f)
    changed = sorted(k for k in set(base) | set(now) if base.get(k) != now.get(k))
    print(json.dumps({"ok": True, "changed": changed}))


if __name__ == "__main__":
    main()

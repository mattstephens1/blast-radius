# pytest plugin: per-test coverage contexts + record files each test opens
import os, sys, json, coverage, pytest
import concurrent.futures as _cf
class _InProc(_cf.ThreadPoolExecutor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        super().__init__(max_workers=max_workers, initializer=initializer, initargs=initargs)
    def submit(self, fn, *args, **kwargs):
        import pickle
        # mimic a process pool: arguments and results cross a pickle boundary
        fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
        def run():
            return pickle.loads(pickle.dumps(fn(*args, **kwargs)))
        return super().submit(run)
_cf.ProcessPoolExecutor = _InProc
import concurrent.futures.process as _cfp; _cfp.ProcessPoolExecutor = _InProc
ROOT = os.path.abspath('.')
cov = coverage.Coverage(data_file='/home/claude/tools/.cov', source=['sqlglot'], branch=False)
OPENED = {}
_cur = [None]
def _hook(ev, args):
    if ev == 'open' and _cur[0] and args and isinstance(args[0], (str, bytes)):
        p = os.fsdecode(args[0])
        p = os.path.abspath(p)
        if p.startswith(ROOT + '/tests/'):
            OPENED.setdefault(_cur[0], set()).add(os.path.relpath(p, ROOT))
sys.addaudithook(_hook)
def pytest_sessionstart(session):
    cov.start()
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _cur[0] = item.nodeid
    cov.switch_context(item.nodeid)
    yield
    _cur[0] = None
    cov.switch_context('')
def pytest_sessionfinish(session):
    cov.stop(); cov.save()
    json.dump({k: sorted(v) for k, v in OPENED.items()}, open('/home/claude/tools/opened.json', 'w'))

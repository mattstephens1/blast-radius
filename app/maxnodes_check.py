"""Can a patch change the outcome of tests/test_parser.py::TestParser::test_max_nodes?

That test (28.8 s) parses one SQL string made of the same expression repeated
10,000 times, with node limits of 1,000,000 and 50,000, and checks a few small
parses around it. Parsing a repetition of identical columns is uniform: each
column adds the same nodes, so the node count is a + b * width. The full-size
run produces exactly 1,000,001 nodes against its 1,000,000 limit, so a change
of even one node in a or b decides the test.

This module redoes every assertion of the test at a size that takes about a
second, with the patched source, and fingerprints the results: the small
parses, the exact node count and tree at two widths (which pins a and b), the
error raised at the 50,000 limit (type, message, position), the max_nodes=5
error and the parse that follows it. If everything matches the unpatched
fingerprint (taken at image build time), the full-size test computes what it
did before, so it passes.

    python maxnodes_check.py --baseline <repo> <out.json>
    python maxnodes_check.py --check <repo> <baseline.json>   (stdin: {path: source})
"""
# sqlglot turns on extra validation when it is imported under a test runner
# (expressions/core.py: UNITTEST = "unittest" in sys.modules or "pytest" in
# sys.modules). Import unittest first so every check sees what the tests see.
import unittest  # noqa: F401
import hashlib
import json
import os
import sys

TEST_ID = "tests/test_parser.py::TestParser::test_max_nodes"
DEPTH = 100          # as in the test
WIDTHS = (50, 100)   # two widths pin both terms of the node count
LIMITED = 600        # enough columns to exceed the test's 50,000 limit


def _setup(repo, overrides):
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if overrides:
        import import_state
        sys.meta_path.insert(0, import_state._Patched(repo, overrides))


def fingerprint():
    from sqlglot import parse_one
    from sqlglot.errors import ParseError
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    parts = []

    def attempt(label, fn):
        try:
            parts.append("%s ok %s" % (label, fn()))
        except ParseError as error:
            parts.append("%s ParseError %s" % (label, error))
        except Exception as error:  # anything else would fail the test differently
            parts.append("%s %s %s" % (label, type(error).__name__, error))

    attempt("select1", lambda: repr(parse_one("SELECT 1", max_nodes=1_000_000)))
    cols = ",".join("col%d" % i for i in range(500))
    attempt("cols500", lambda: repr(parse_one("SELECT %s FROM t" % cols, max_nodes=1_000_000)))
    expr = "x" + "=x" * DEPTH
    for width in WIDTHS:
        sql = "SELECT " + ",".join([expr] * width)

        def counted(sql=sql):
            parser = Parser(max_nodes=10 ** 9)
            trees = parser.parse(Tokenizer().tokenize(sql), sql)
            return "%d %s" % (parser._node_count, hashlib.blake2b(repr(trees).encode(), digest_size=12).hexdigest())

        attempt("width%d" % width, counted)
    sql = "SELECT " + ",".join([expr] * LIMITED)
    attempt("limit50k", lambda: repr(parse_one(sql, max_nodes=50_000)))
    attempt("limit5", lambda: repr(parse_one("SELECT " + ",".join("x=%d" % i for i in range(100)), max_nodes=5)))
    attempt("after", lambda: repr(parse_one("SELECT 1, 2, 3")))
    return hashlib.blake2b("\n".join(parts).encode(), digest_size=16).hexdigest(), parts


def main():
    mode, repo, path = sys.argv[1:4]
    repo = os.path.abspath(repo)
    if mode == "--baseline":
        _setup(repo, None)
        digest, parts = fingerprint()
        with open(path, "w") as f:
            json.dump({"fingerprint": digest}, f)
        return
    overrides = json.load(sys.stdin)
    with open(path) as f:
        base = json.load(f)["fingerprint"]
    try:
        _setup(repo, overrides)
        digest, _ = fingerprint()
    except BaseException as error:
        print(json.dumps({"ok": False, "error": repr(error)[:300]}))
        return
    print(json.dumps({"ok": True, "same": digest == base}))


if __name__ == "__main__":
    main()

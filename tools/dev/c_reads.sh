#!/bin/sh
# Run-time reads of class/module containers that bypass the read map's recording
# (C fast paths: set()/frozenset() copies, dict-view algebra, set methods taking
# the container as argument). Review hits that run at run time (not in a
# metaclass or class body) and add their names to READ_BY_C in app/candidates.py.
grep -rnE "(frozenset|set|dict\.fromkeys)\((self|cls|exp|type\(self\)|[a-z_]+)\.[A-Z][A-Z0-9_]{2,}\)|\.keys\(\) *[-&|^] *(self|cls|[a-z_]+)\.[A-Z]|\{\*(self|cls|[a-z_]+)\.[A-Z]|\.(union|intersection|difference|issubset|issuperset|isdisjoint|update)\(\*?(self|cls|[a-z_]+)\.[A-Z][A-Z0-9_]{2,}" "${1:-sqlglot_src}/sqlglot"

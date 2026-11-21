# usage: label.sh <patch...> ; prints "<id> <failing nodeid>" lines and "<id> DONE"
# Parallel run to find candidates, then a serial re-run of those to confirm:
# under memory pressure xdist workers crash and their tests are reported failed.
ids() { sed -n "/short test summary info/,\$p" | grep -oE "tests/[A-Za-z0-9_/]+\.py::[A-Za-z0-9_:]+" | sort -u; }
for p in "$@"; do
  id=$(basename "$p" .patch)
  rm -rf /work && cp -r /sqlglot /work && cd /work
  if [ -s "$p" ] && ! patch -p1 -s < "$p" >/dev/null 2>&1; then echo "$id PATCHFAIL"; continue; fi
  timeout 1200 python -m pytest -q -p no:cacheprovider -n 3 -o addopts= --timeout=60 tests -rfE 2>&1 | ids > /tmp/cand
  if [ -s /tmp/cand ]; then
    timeout 1200 python -m pytest -q -p no:cacheprovider -o addopts= --timeout=60 -rfE $(cat /tmp/cand) 2>&1 | ids | sed "s/^/$id /"
  fi
  echo "$id DONE"
done

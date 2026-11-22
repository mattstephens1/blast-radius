# each line of /d/dropped.txt: <commit> <test id>...; prints "<commit> FAIL <id>" for dropped tests that fail, then "<commit> DONE"
while read c ids; do
  [ -z "$c" ] && continue
  rm -rf /work && cp -r /sqlglot /work && cd /work
  patch -p1 -s < /p/$c.patch || { echo "$c PATCHFAIL"; continue; }
  timeout 900 python -m pytest -q -p no:cacheprovider -o addopts= --timeout=300 -rfE $ids 2>&1 \
    | sed -n "/short test summary info/,\$p" | grep -oE "tests/[A-Za-z0-9_/]+\.py::[A-Za-z0-9_:]+" | sort -u | sed "s/^/$c FAIL /"
  echo "$c DONE"
done < /d/dropped.txt

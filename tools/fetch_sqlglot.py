"""Build step: download sqlglot's source at the pinned commit into /opt/sqlglot."""
import io
import tarfile
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz"

data = urllib.request.urlopen(URL, timeout=300).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    # the package, plus the test modules, plus the TPC fixtures that
    # app/tpc_check.py replays (not the rest of the fixture data)
    tpc = ("tests/fixtures/optimizer/tpc-ds/", "tests/fixtures/optimizer/tpc-h/")
    keep = [m for m in tar.getmembers()
            if m.name.split("/")[1:2] == ["sqlglot"]
            or (m.name.split("/")[1:2] == ["tests"] and m.name.endswith(".py"))
            or (m.isfile() and m.name.split("/", 1)[1].startswith(tpc))]
    for m in keep:
        m.name = m.name.split("/", 1)[1]
    tar.extractall("/opt/sqlglot", members=keep, filter="data")
print(f"extracted {len(keep)} files")

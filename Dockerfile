# Same base image the organisers used to label every commit.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot's own source at the pinned commit (allowed by the rules), so the
# selector can compare each patch's old and new code.
COPY tools/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py && test -f /opt/sqlglot/sqlglot/parser.py

COPY selector.py /app/selector.py
COPY app /app/app
COPY maps /app/maps
# Fingerprint of sqlglot's import-time state, taken here so it matches the
# runtime interpreter exactly (the build is not charged). See app/import_state.py.
RUN PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
    python /app/app/import_state.py --baseline /opt/sqlglot /app/maps/import_state.json \
    && test -s /app/maps/import_state.json
# Fingerprints of what the TPC executor tests prepare from each query (see app/tpc_check.py).
RUN PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
    python /app/app/tpc_check.py --baseline /opt/sqlglot /opt/sqlglot/tests/fixtures /app/maps/tpc_state.json \
    && test -s /app/maps/tpc_state.json
# Fingerprint of what test_max_nodes checks, at reduced size (see app/maxnodes_check.py).
RUN PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
    python /app/app/maxnodes_check.py --baseline /opt/sqlglot /app/maps/maxnodes.json \
    && test -s /app/maps/maxnodes.json
# The selector's word index over sqlglot's source, so runs load it (0.1 s) instead of
# building it (0.26 s); CPU time counts toward the score.
RUN cd /app/app && python -c "import candidates; candidates.write_index('/opt/sqlglot', '/app/maps/index.marshal')" \
    && test -s /app/maps/index.marshal
ENV SQLGLOT_DIR=/opt/sqlglot PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]

# Same base image the organisers used to label every commit.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot's own source at the pinned commit (allowed by the rules), so the
# selector can compare each patch's old and new code.
COPY tools/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py && test -f /opt/sqlglot/sqlglot/parser.py

COPY selector.py /app/selector.py
COPY app /app/app
COPY maps /app/maps
ENV SQLGLOT_DIR=/opt/sqlglot PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends patch && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt
RUN python -c "import io,tarfile,urllib.request; d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); t=tarfile.open(fileobj=io.BytesIO(d)); t.extractall('/src', filter='data')" && mv /src/sqlglot-* /sqlglot
COPY label.sh /label.sh
WORKDIR /sqlglot
ENTRYPOINT ["sh", "/label.sh"]

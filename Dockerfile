FROM python:3.12-slim

WORKDIR /app

# Patch base-image decoder/installer before installing application packages.
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade liblzma5 && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 && pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 monarch \
    && useradd --uid 10001 --gid 10001 --create-home monarch \
    && mkdir /data \
    && chown 10001:10001 /data \
    && python -m pip uninstall -y pip

COPY server.py transaction_tools.py rule_management.py attachments.py security.py .

EXPOSE 8000
USER 10001:10001
ENV FASTMCP_HOME=/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"

CMD ["python", "server.py"]

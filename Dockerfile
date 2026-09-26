FROM python:3.12-slim

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 monarch \
    && useradd --uid 10001 --gid 10001 --create-home monarch \
    && mkdir /data \
    && chown 10001:10001 /data

COPY server.py .

EXPOSE 8000
USER 10001:10001
ENV FASTMCP_HOME=/data

CMD ["python", "server.py"]

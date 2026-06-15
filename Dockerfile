FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh

RUN useradd -r -u 1000 -g root mediatriage \
    && mkdir -p /data \
    && chown -R mediatriage /app \
    && chmod +x /entrypoint.sh

EXPOSE 5543

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5543", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

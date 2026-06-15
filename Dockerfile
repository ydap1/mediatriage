FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -r -u 1000 -g root mediatriage \
    && mkdir -p /data \
    && chown -R mediatriage /app /data
USER mediatriage

EXPOSE 5543

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5543", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

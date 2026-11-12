FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --uid 1000 --no-create-home radar && mkdir /data && chown radar:radar /data
USER radar
VOLUME /data
EXPOSE 8097

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/healthz', timeout=4)"

# --proxy-headers lets the app see the real client IP and https scheme behind Nginx Proxy Manager
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097", "--proxy-headers", "--forwarded-allow-ips", "*"]

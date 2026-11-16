FROM python:3.12-slim

RUN useradd --create-home --uid 1000 broker && mkdir /data && chown broker /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY camels_broker ./camels_broker

USER broker
EXPOSE 8000
# Pings every 20 s; no pong within 20 s closes the connection (protocol section 3).
CMD ["uvicorn", "camels_broker.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--ws", "websockets", "--ws-max-size", "1100000", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "20"]

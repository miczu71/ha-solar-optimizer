ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.sh /
RUN chmod +x /run.sh

COPY src/ ./src/

CMD ["/run.sh"]

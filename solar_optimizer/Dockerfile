ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip py3-numpy py3-scipy

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY run.sh /
RUN chmod +x /run.sh

COPY src/ ./src/

CMD ["/run.sh"]

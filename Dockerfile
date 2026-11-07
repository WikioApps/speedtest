FROM alpine:latest

RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir speedtest-cli --break-system-packages

CMD ["speedtest-cli"]

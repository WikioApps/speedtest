FROM alpine:latest

RUN apk add --no-cache curl

CMD ["sh", "-c", "echo '=== VPS Network & ISP Details ===' && curl -s https://ipinfo.io/json"]

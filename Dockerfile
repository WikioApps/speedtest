FROM alpine:latest

RUN apk add --no-cache curl && curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.alpine.sh | sh && apk add --no-cache speedtest

CMD ["speedtest", "--accept-license", "--accept-gdpr"]

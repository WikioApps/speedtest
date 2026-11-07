FROM alpine:latest

# Install dependencies and Ookla Speedtest CLI
RUN apk add --no-cache curl && \
    curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.alpine.sh | sh && \
    apk add --no-cache speedtest

# Run speedtest non-interactively, accepting the license and GDPR notice
CMD ["speedtest", "--accept-license", "--accept-gdpr"]

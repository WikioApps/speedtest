FROM alpine:latest

RUN apk add --no-cache rclone

# Keeps the container alive so you can execute or schedule rclone sync jobs
CMD ["tail", "-f", "/dev/null"]

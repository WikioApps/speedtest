FROM alpine:latest

RUN apk add --no-cache tinyproxy

# Configure tinyproxy to allow connections from anywhere (or restrict it if needed)
RUN sed -i 's/^Allow /#Allow /' /etc/tinyproxy/tinyproxy.conf

EXPOSE 8888

CMD ["tinyproxy", "-d"]

FROM alpine:latest

RUN apk add --no-cache curl bash && \
    curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash

EXPOSE 8080

CMD ["filebrowser", "-r", "/srv", "-a", "0.0.0.0", "-p", "8080"]

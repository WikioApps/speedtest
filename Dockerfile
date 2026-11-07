FROM alpine:latest

# Install PHP and essential modules
RUN apk add --no-cache nginx php82 php82-fpm php82-session php82-ctype php82-json php82-mbstring curl && \
    mkdir -p /var/www/html && \
    curl -sL https://raw.githubusercontent.com/prasathmani/tinyfilemanager/master/tinyfilemanager.php -o /var/www/html/index.php && \
    sed -i 's/listen = 127.0.0.1:9000/listen = \/var\/run\/php-fpm.sock/g' /etc/php82/php-fpm.d/www.conf

# Configure Nginx
RUN echo "server { listen 80; root /var/www/html; index index.php; location / { try_files \$uri \$uri/ /index.php?\$args; } location ~ \.php\$ { include fastcgi_pass unix:/var/run/php-fpm.sock; include fastcgi.conf; } }" > /etc/nginx/http.d/default.conf

EXPOSE 80

CMD ["sh", "-c", "php-fpm82 && nginx -g 'daemon off;'"]

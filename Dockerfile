FROM alpine:latest

# Install Nginx and standard PHP packages available in Alpine
RUN apk add --no-cache nginx php php-fpm php-session php-ctype php-json php-mbstring curl && \
    mkdir -p /var/www/html && \
    curl -sL https://raw.githubusercontent.com/prasathmani/tinyfilemanager/master/tinyfilemanager.php -o /var/www/html/index.php && \
    sed -i 's/listen = 127.0.0.1:9000/listen = \/var\/run\/php-fpm.sock/g' /etc/php*/php-fpm.d/www.conf

# Configure Nginx to route PHP through the socket
RUN echo "server { listen 80; root /var/www/html; index index.php; location / { try_files \$uri \$uri/ /index.php?\$args; } location ~ \.php\$ { include fastcgi_pass unix:/var/run/php/php-fpm.sock; include fastcgi.conf; fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name; } }" > /etc/nginx/http.d/default.conf

EXPOSE 80

CMD ["sh", "-c", "php-fpm && nginx -g 'daemon off;'"]

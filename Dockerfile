FROM php:8.2-apache

# Install curl and unzip to fetch Tiny File Manager
RUN apt-get update && apt-get install -y curl unzip && rm -rf /var/lib/apt/lists/*

# Download and set up Tiny File Manager as the default web root index
RUN curl -sL https://raw.githubusercontent.com/prasathmani/tinyfilemanager/master/tinyfilemanager.php -o /var/www/html/index.php

# Force Apache to explicitly listen on port 80
RUN echo "Listen 80" > /etc/apache2/ports.conf

# Expose port 80
EXPOSE 80

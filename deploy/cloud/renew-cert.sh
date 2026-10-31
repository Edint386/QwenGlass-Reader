#!/bin/sh
# 续期证书（glassreader-cert-renew.timer 每周跑一次）。没到续期时间时 certbot 什么也不做。
set -eu

domain="reader.example.com"
deploy_root="/opt/glassreader"
openresty_root="/opt/1panel/apps/openresty/openresty"
site_ssl="$openresty_root/www/sites/$domain/ssl"

docker run --rm \
  -v "$deploy_root/letsencrypt:/etc/letsencrypt" \
  -v "$openresty_root/root:/var/www/acme" \
  certbot/certbot:latest renew \
  --webroot \
  --webroot-path /var/www/acme \
  --quiet

install -m 0644 "$deploy_root/letsencrypt/live/$domain/fullchain.pem" "$site_ssl/fullchain.pem"
install -m 0600 "$deploy_root/letsencrypt/live/$domain/privkey.pem" "$site_ssl/privkey.pem"

docker exec 1Panel-openresty-XXXX openresty -t
docker exec 1Panel-openresty-XXXX openresty -s reload

#!/bin/sh
set -eu

cd /opt/acc-system

# Renew only when the certificate has less than 30 days remaining.
if openssl x509 -checkend 2592000 -noout -in certs/live/folio.sarl/fullchain.pem >/dev/null; then
    exit 0
fi

docker run --rm -v /opt/acc-system/certs:/etc/letsencrypt \
    -v /opt/acc-system/acme:/var/www/acme \
    certbot/certbot:latest renew --cert-name folio.sarl --webroot -w /var/www/acme --non-interactive
docker exec acc-system-nginx-1 nginx -s reload

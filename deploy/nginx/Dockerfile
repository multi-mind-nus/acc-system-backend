FROM nginx:1.28.0-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY app-locations.conf /etc/nginx/snippets/app-locations.conf

RUN mkdir -p /etc/nginx/tls

EXPOSE 80 443

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

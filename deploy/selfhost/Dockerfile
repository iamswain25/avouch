# syntax=docker/dockerfile:1
FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="avouch-site" \
      org.opencontainers.image.description="Avouch — 보증이 편집권이 되는 게시 프로토콜 (landing + whitepaper)" \
      org.opencontainers.image.source="https://github.com/…/avouch"

RUN rm -rf /usr/share/nginx/html/*
COPY deploy/selfhost/nginx.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]

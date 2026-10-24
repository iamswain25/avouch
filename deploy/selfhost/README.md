# Self-host bundle (not part of development)

The primary deployment is Cloudflare Pages (static site + Functions + D1). This directory
exists only for two cases:

1. Self-hosting the static site on your own box (nginx container).
2. Reproducing the built output outside Cloudflare.

It is not used in the development loop. Edge development uses `wrangler pages dev`, which
runs the same workerd runtime as production without any container.

    docker compose -f deploy/selfhost/docker-compose.yml up -d --build    # http://localhost:8787
    docker compose -f deploy/selfhost/docker-compose.yml down

The build context is the repository root, so `site/` and `deploy/selfhost/nginx.conf` are
copied from their real locations. Rebuild after `python scripts/build_whitepaper.py`.

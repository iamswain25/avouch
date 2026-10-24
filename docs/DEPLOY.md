# Avouch — 도메인 연결과 배포

선정 도메인: `avouchprotocol.com` ($11.08/년, Porkbun, 갱신 동일)
보조 후보: `avouch.kr` (₩16,500/년, 가비아 등 국내 등록기관)

## 1. 도메인 구입 후 DNS

가장 단순한 구성은 A 레코드 하나다.

| 타입 | 이름 | 값 | TTL |
| --- | --- | --- | --- |
| A | `@` | 서버 공인 IP | 300 |
| A | `www` | 서버 공인 IP | 300 |

서버 없이 먼저 공개만 하려면 Cloudflare Pages/Vercel에 `site/`를 올리고 CNAME을 걸어도 된다(정적 사이트라 그대로 동작).

## 2. TLS

- Caddy를 앞에 두는 방식이 가장 짧다.

      avouchprotocol.com {
          reverse_proxy 127.0.0.1:8787
      }

- 또는 Cloudflare 프록시(주황 구름)를 켜고 오리진은 8787 그대로 둔다. 이 경우 compose 포트를 `127.0.0.1:8787:80`으로 바꿔 외부 노출을 막는다.

## 3. go-live 체크리스트

    docker compose ps                     # healthy
    curl -I https://avouchprotocol.com/   # 200, Strict-Transport 확인
    curl -s https://avouchprotocol.com/whitepaper.html | head -3
    www -> apex 301 확인
    http -> https 301 확인

## 4. nginx 설정 메모

`nginx.conf`의 `server_name`에 `avouchprotocol.com www.avouchprotocol.com _;`를 넣어 두었다. 단일 server 블록이라 unmatched Host도 이 블록이 받으므로 localhost:8787 테스트는 그대로 동작한다.

## 5. 배포 파이프라인(권장)

    uv run --with markdown python scripts/build_whitepaper.py --sync
    docker build -t avouch-site:$(git rev-parse --short HEAD) .
    docker tag avouch-site:$(git rev-parse --short HEAD) avouch-site:latest
    docker compose up -d --force-recreate

서버에서는 `docker save | ssh docker load` 또는 레지스트리 push/pull. 태그는 git sha로 고정한다.

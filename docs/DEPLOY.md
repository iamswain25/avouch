# Avouch — 도메인 연결과 배포

도메인: `avouchprotocol.com` (Dynadot 등록 완료)
사이트 구조: `/ko/`, `/en/` 서브패스 + 루트 로케일 라우팅

## 1. Dynadot DNS

Dynadot → My Domains → `avouchprotocol.com` → DNS Settings

가장 단순한 구성(A 레코드):

| 타입 | 호스트 | 값 | TTL |
| --- | --- | --- | --- |
| A | `@` (빈 값) | 서버 공인 IP | 300 |
| A | `www` | 서버 공인 IP | 300 |

또는 `www`를 CNAME으로 `avouchprotocol.com`에 걸어도 된다.

Cloudflare를 쓸 경우: Dynadot에서 네임서버를 Cloudflare NS 2개로 위임 → Cloudflare에서 위 레코드를 만들고 프록시(주황 구름)를 켠다. 이때 오리진은 8787을 외부에 열지 말고 `127.0.0.1:8787:80`으로 바인딩한다.

## 2. TLS

오리진 앞에 Caddy를 두는 방식이 가장 짧다.

    avouchprotocol.com, www.avouchprotocol.com {
        reverse_proxy 127.0.0.1:8787
    }

Cloudflare 프록시를 쓰면 SSL 모드를 Full (strict)로 두고, 오리진에는 Caddy 또는 Cloudflare Origin Certificate를 설치한다. Flexible은 리다이렉트 루프의 원인이 되므로 쓰지 않는다.

## 3. 프록시 헤더 (locale 라우팅이 깨지지 않게)

nginx는 `Accept-Language`와 `lang` 쿠키를 읽어 `/`를 302로 분기한다. 프록시가 헤더를 지우면 항상 기본값(en)으로 떨어진다.

- Caddy: 기본적으로 Host와 Accept-Language를 그대로 전달한다.
- Cloudflare: `Accept-Language`는 전달되지만, 캐시 규칙이 `/`를 캐시하면 안 된다(`Cache-Control: no-store`가 이미 붙어 있다). Cloudflare Cache Rule에서 `/`를 bypass로 명시하는 편이 안전하다.

## 4. go-live 체크리스트

    docker compose ps                                  # healthy
    curl -I https://avouchprotocol.com/                # 302 -> /en/ 또는 /ko/
    curl -I -H 'Accept-Language: ko-KR' https://avouchprotocol.com/   # 302 -> /ko/
    curl -s -o /dev/null -w '%{http_code}\n' https://avouchprotocol.com/ko/
    curl -s -o /dev/null -w '%{http_code}\n' https://avouchprotocol.com/en/whitepaper.html
    curl -s https://avouchprotocol.com/robots.txt
    www -> apex 301 확인, http -> https 301 확인
    브라우저 언어를 영어/한국어로 바꿔 루트 진입 확인

## 5. nginx 설정 메모

- `server_name avouchprotocol.com www.avouchprotocol.com _;` — 단일 server 블록이라 unmatched Host도 이 블록이 받는다(localhost 테스트 유지).
- `absolute_redirect off;` — TLS 종단 프록시 뒤에서 `Location: /ko/` 같은 상대 경로가 나가야 http/https 루프가 없다.
- `map` 두 개는 `conf.d/default.conf`의 server 블록 밖(http 컨텍스트)에 있어야 한다.

## 6. 배포 파이프라인(권장)

    uv run --with markdown python scripts/build_whitepaper.py --sync
    docker build -t avouch-site:$(git rev-parse --short HEAD) .
    docker tag avouch-site:$(git rev-parse --short HEAD) avouch-site:latest
    docker compose up -d --force-recreate

서버에서는 `docker save | ssh docker load` 또는 레지스트리 push/pull. 태그는 git sha로 고정한다.

## 7. 검색엔진

- `/ko/`, `/en/` 모두 hreflang(`ko`, `en`, `x-default`)을 갖는다. 루트 `/`는 라우터라 `noindex`.
- `sitemap.xml`, `robots.txt` 포함. Search Console에 `avouchprotocol.com` 등록 후 sitemap 제출.

# Avouch — 배포 (Cloudflare Pages)

도메인: `avouchprotocol.com` (Dynadot 등록 완료)
호스팅: Cloudflare Pages (정적) + Pages Function 1개(로케일 라우팅)
사이트 구조: `/ko/`, `/en/` 서브패스, 루트 `/`는 언어 분기

## 1. Cloudflare로 네임서버 위임

Pages 커스텀 도메인은 해당 존이 Cloudflare에 있어야 한다.

1. Cloudflare 대시보드 → Add a site → `avouchprotocol.com` (Free)
2. 표시되는 네임서버 2개를 Dynadot → My Domains → avouchprotocol.com → Name Servers 에서 교체
3. 전파 확인(보통 수 분~수 시간). Cloudflare가 Active 되면 다음 단계

## 2. Pages 프로젝트 (Git 연동 권장)

Cloudflare → Workers & Pages → Create → Pages → Connect to Git → 저장소 `avouch` 선택

| 설정 | 값 |
| --- | --- |
| Framework preset | None |
| Build command | (비움) — 생성된 HTML이 저장소에 커밋되어 있다 |
| Build output directory | `site` |
| Root directory | 저장소 루트 (functions/ 를 자동 인식) |

`functions/` 디렉터리가 루트에 있으면 Pages Functions로 배포된다. 라우팅은 `site/_routes.json`이 `/`만 함수로 보내고 나머지는 정적 파일로 처리한다.

빌드 단계에서 백서를 생성하고 싶으면 Build command를 `python3 -m pip install markdown && python3 scripts/build_whitepaper.py`로 두고 환경변수 `PYTHON_VERSION=3.12`를 넣는다. 실패 시 (비움)으로 되돌리면 된다 — 산출물이 커밋되어 있기 때문이다.

## 3. 직접 업로드 (Git 없이)

    cd avouch
    npx wrangler login
    npx wrangler pages deploy site --project-name avouch

`wrangler pages deploy`는 현재 디렉터리의 `functions/`를 함께 올린다. 프로젝트 루트에서 실행할 것.

## 4. 커스텀 도메인

Pages → 프로젝트 → Custom domains → `avouchprotocol.com` 추가 (apex는 자동으로 CNAME flattening). `www.avouchprotocol.com`도 추가한 뒤, www → apex 301을 원하면 Cloudflare Rules → Redirect Rules에서:

    호스트가 www.avouchprotocol.com 이면 301 → https://avouchprotocol.com/$1

## 4-1. 보안 설정 (적용 완료)

| 항목 | 값 | 위치 |
| --- | --- | --- |
| HTTPS 강제 | http → https 301 | Cloudflare 존 설정 |
| HSTS | max-age=15552000 (180일) | `site/_headers` + 미들웨어 응답 |
| CSP | `default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'self'; ...` | `site/_headers` |
| X-Content-Type-Options / Referrer-Policy / X-Frame-Options | nosniff / strict-origin-when-cross-origin / SAMEORIGIN | `_headers` |
| Permissions-Policy | geolocation·microphone·camera·payment 차단 | `_headers` |
| COOP | same-origin | `_headers` |
| 정식 호스트 단일화 | www → apex 301, `*.pages.dev` → apex 301 | `functions/_middleware.js` |
| 인라인 JS 제거 | 스크립트는 `assets/lang.js`, `assets/route.js` (CSP `script-src 'self'` 유지) | `site/` |

주의: `_headers`는 Function이 반환하는 응답에 적용되지 않는다. 리다이렉트 응답에도 HSTS를 넣어야 하면 미들웨어의 `Response` 헤더에 직접 넣는다(현재 그렇게 되어 있다).

시크릿 점검(배포 전 반복):

    grep -rn --exclude-dir=.git -E "cfut_[A-Za-z0-9_-]{10,}|BEGIN .*PRIVATE KEY" . || echo clean
    git log -p --all | grep -cE "cfut_[A-Za-z0-9_-]{10,}|BEGIN .*PRIVATE KEY"   # 0이어야 함

## 4-2. 남은 수동 설정 (대시보드 1회)

- Pages → avouch → Settings → Builds & deployments → Preview deployments: **None** 권장. 배포마다 공개 URL(`<id>.avouch-cwh.pages.dev`)이 생기는데, 미들웨어가 apex로 301하도록 해두었지만 미리보기 자체를 끄는 편이 표면이 작다.
- API 토큰(`avouch-dns-automation`, Zone·DNS·Edit)은 작업이 끝나면 Revoke 하거나 TTL을 짧게 유지한다.

## 5. 로케일 라우팅 (중요)

- `functions/index.js`가 `/`만 처리한다: cookie `lang` → Accept-Language(q 순) → 기본 `en`. `Location: /ko/` 또는 `/en/`, `Vary: Accept-Language, Cookie`, `Cache-Control: no-store`.
- 정적 파일은 Functions를 타지 않는다(`_routes.json`).
- Cloudflare Pages는 `.html` 요청을 확장자 없는 URL로 308 리다이렉트한다. 그래서 canonical은 `/ko/whitepaper`(확장자 없음)이고, 사이트 내 링크·hreflang·sitemap을 모두 그 형태로 맞춰두었다. 파일명은 `whitepaper.html` 그대로다.
- nginx(로컬/도커)도 같은 동작이 되도록 `try_files $uri $uri.html $uri/`를 넣어 두었다. 두 호스트의 URL이 동일하다.

## 6. 헤더·리다이렉트·SEO

- `site/_headers`: 보안 헤더, HTML `no-cache`, `/assets/*` 7일 immutable
- `site/_redirects`: `/ko` `/en` → 슬래시 301
- `site/_routes.json`: 함수 호출 범위 제한
- hreflang(`ko`, `en`, `x-default`), `sitemap.xml`, `robots.txt` 포함. 루트 `/`는 라우터라 `noindex`
- Search Console에 `avouchprotocol.com` 등록 후 `https://avouchprotocol.com/sitemap.xml` 제출

## 7. go-live 체크리스트

    curl -I https://avouchprotocol.com/                                  # 302 -> /en/ 또는 /ko/
    curl -I -H 'Accept-Language: ko-KR' https://avouchprotocol.com/      # 302 -> /ko/
    curl -s -o /dev/null -w '%{http_code}\n' https://avouchprotocol.com/ko/
    curl -s -o /dev/null -w '%{http_code}\n' https://avouchprotocol.com/ko/whitepaper
    curl -s -o /dev/null -w '%{http_code}\n' https://avouchprotocol.com/en/whitepaper
    curl -s https://avouchprotocol.com/robots.txt
    curl -sI https://avouchprotocol.com/assets/style.css | grep -i cache-control
    www -> apex 301 확인
    브라우저 언어를 영어/한국어로 바꿔 루트 진입 확인

## 8. 도커 (셀프호스트 대안)

VPS에 올릴 경우를 위해 같은 산출물을 nginx 이미지로도 서빙한다.

    docker build -q -t avouch-site:dev .
    docker compose -f deploy/selfhost/docker-compose.yml up -d --build   # http://localhost:8787

`nginx.conf`는 Pages와 동일한 URL 체계(확장자 없는 whitepaper, `/` 로케일 302)를 흉내 낸다. 프록시 뒤에서는 `absolute_redirect off` 덕분에 `Location`이 상대 경로로 나간다.

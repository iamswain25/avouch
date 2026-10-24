# Avouch — site

보증이 편집권이 되는 게시 프로토콜. 랜딩 + 백서, 한국어/영어 2개 언어, nginx 컨테이너 서빙.

- 프로덕션 도메인: `avouchprotocol.com` (Dynadot 등록)
- 로컬: http://localhost:8787

## 도커 (기본)

    cd ~/workspace/avouch
    docker build -q -t avouch-site:dev .
    docker compose up -d --force-recreate
    docker compose ps            # healthy 확인
    docker compose logs -f
    docker compose down

호스트 8787 -> 컨테이너 80. 포트는 `docker-compose.yml`에서 바꾼다.

## 콘텐츠 갱신

    uv run --with markdown python scripts/build_whitepaper.py --sync   # 위키(ko) -> content/ko -> site/ko, site/en
    docker build -q -t avouch-site:dev . && docker compose up -d --force-recreate

- 한국어 백서: 위키 마스터 `~/Documents/crypto/research/avouch-whitepaper-2026-10.md` 에서 `--sync`로 가져온다.
- 영어 백서: 이 저장소의 `content/en/whitepaper.md` 를 직접 관리한다(동기화 대상 아님). 한국어 마스터의 축약·번역판이며, 수치는 동일하게 유지한다.
- 랜딩(`site/ko/index.html`, `site/en/index.html`)은 손으로 쓴 정적 HTML이다. 수치가 바뀌면 두 파일을 같이 고친다.

## 언어 라우팅 (subpath)

    /            -> 302  (cookie lang 우선, 없으면 Accept-Language, 기본 en)
    /ko/         한국어 랜딩, /ko/whitepaper.html
    /en/         영어 랜딩,  /en/whitepaper.html

- `nginx.conf`의 `map $cookie_lang` + `map $http_accept_language` 가 `/`를 분기한다. `Vary: Accept-Language, Cookie`, `Cache-Control: no-store`.
- 페이지의 언어 링크는 `lang` 쿠키와 `localStorage.avouch_lang`을 함께 세팅한다.
- nginx가 없는 호스트(Cloudflare Pages, Vercel)에서는 루트 `site/index.html`의 JS 라우터가 같은 규칙으로 보낸다.
- `absolute_redirect off` — TLS 종단 프록시 뒤에서 `Location`이 상대 경로로 나가야 http/https 루프가 안 생긴다.
- hreflang(`ko`, `en`, `x-default`) + `sitemap.xml` + `robots.txt` 포함.

## 구조

    Dockerfile                 nginx:1.27-alpine, site/ 복사, HEALTHCHECK
    nginx.conf                 locale map, 캐시 정책, gzip, 보안 헤더
    docker-compose.yml         avouch-site, 8787:80, restart unless-stopped
    site/index.html            로케일 라우터 (JS 폴백)
    site/ko/index.html         한국어 랜딩
    site/ko/whitepaper.html    한국어 백서 (생성물)
    site/en/index.html         영어 랜딩
    site/en/whitepaper.html    영어 백서 (생성물)
    site/assets/style.css      공용 스타일
    content/ko/whitepaper.md   위키 마스터 사본
    content/en/whitepaper.md   영어판 원본
    docs/DEPLOY.md             Dynadot DNS, TLS, go-live 체크리스트
    scripts/build_whitepaper.py

## 배포

`docs/DEPLOY.md` 참고. 요약: Dynadot에서 A 레코드(또는 Cloudflare NS 위임) → 리버스 프록시(Caddy/Cloudflare) 뒤에 컨테이너를 두고 8787은 외부에 열지 않는다. 이미지 태그는 `avouch-site:<git-sha>`.

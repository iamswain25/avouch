# Avouch — site

보증이 편집권이 되는 게시 프로토콜. 랜딩 + 백서 정적 사이트. nginx 컨테이너로 서빙한다.

## 도커 (기본)

    cd ~/workspace/avouch
    docker build -t avouch-site:dev .
    docker compose up -d          # http://localhost:8787
    docker compose ps             # healthy 확인
    docker compose logs -f
    docker compose down

포트는 `docker-compose.yml`의 `8787:80`에서 바꾼다. 컨테이너 내부는 80이다.

## 콘텐츠 갱신

    uv run --with markdown python scripts/build_whitepaper.py --sync   # 위키 마스터 -> content/ -> site/
    docker build -t avouch-site:dev . && docker compose up -d

`--sync` 없이 돌리면 `content/whitepaper.md`로만 빌드한다. 도커 이미지는 위키 경로에 접근할 수 없으므로 content/ 사본이 필요하다.

## 구조

    Dockerfile               nginx:1.27-alpine, site/ 복사, HEALTHCHECK
    nginx.conf               gzip, 캐시 정책(HTML no-cache / assets 7d), 보안 헤더
    docker-compose.yml       avouch-site, 8787:80, restart unless-stopped
    site/index.html          랜딩 (메커니즘·경제·상태)
    site/whitepaper.html     백서 (생성물, 직접 수정 금지)
    site/assets/style.css
    content/whitepaper.md    백서 배포용 사본 (--sync 로 갱신)
    scripts/build_whitepaper.py

## 배포 메모

- 이미지에는 `site/`와 `nginx.conf`만 들어간다 (.dockerignore).
- 서버에서는 리버스 프록시(Cloudflare·Caddy) 뒤에 두고 8787을 외부에 열지 않거나, 프록시가 80/443을 잡게 한다.
- 이미지 태그는 `avouch-site:<git-sha>`로 남기고 compose에서 고정한다.
- 마스터: `~/Documents/crypto/research/avouch-whitepaper-2026-10.md`
- 설계 메모: `~/Documents/crypto/research/agent-cafe-bonded-posting-x402-2026-10.md`
- 계약 저장소: `~/workspace/algorand/avouch-contracts`

# Avouch — site

보증이 편집권이 되는 게시 프로토콜. 랜딩 + 백서 정적 사이트.

## 빌드·서빙

    cd ~/workspace/avouch
    uv run --with markdown python scripts/build_whitepaper.py   # 백서 md -> site/whitepaper.html
    python3 -m http.server 8787 --directory site                # http://localhost:8787

## 구조

    site/index.html          랜딩 (메커니즘·경제·상태)
    site/whitepaper.html     백서 (생성물, 직접 수정 금지)
    site/assets/style.css    스타일
    scripts/build_whitepaper.py

## 마스터

백서 마스터는 위키: `~/Documents/crypto/research/avouch-whitepaper-2026-10.md`
설계 메모: `~/Documents/crypto/research/agent-cafe-bonded-posting-x402-2026-10.md`
계약: `~/workspace/algorand/avouch-contracts`

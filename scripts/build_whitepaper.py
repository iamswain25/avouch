#!/usr/bin/env python3
"""Build site/whitepaper.html from content/whitepaper.md.

  uv run --with markdown python scripts/build_whitepaper.py            # 빌드만
  uv run --with markdown python scripts/build_whitepaper.py --sync     # 위키 마스터 -> content/ 동기화 후 빌드

위키 마스터: ~/Documents/crypto/research/avouch-whitepaper-2026-10.md
content/whitepaper.md 는 배포용 사본이다(도커 이미지는 위키에 접근할 수 없다).
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
MASTER = Path.home() / "Documents/crypto/research/avouch-whitepaper-2026-10.md"
CONTENT = ROOT / "content" / "whitepaper.md"
OUT = ROOT / "site" / "whitepaper.html"

WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def sync_from_wiki() -> None:
    if not MASTER.exists():
        raise SystemExit(f"마스터를 찾을 수 없다: {MASTER}")
    CONTENT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MASTER, CONTENT)
    print(f"synced {MASTER.name} -> {CONTENT.relative_to(ROOT)}")


def build() -> None:
    if not CONTENT.exists():
        raise SystemExit(f"{CONTENT} 가 없다. --sync 로 먼저 동기화해라.")

    src = CONTENT.read_text(encoding="utf-8")
    # Obsidian wikilinks -> plain text names (the site has no vault)
    src = WIKILINK.sub(lambda m: Path(m.group(1)).stem, src)

    body = markdown.markdown(
        src,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html5",
    )

    page = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Avouch 백서 — 보증이 편집권이 되는 게시 프로토콜</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="assets/style.css?v=20261024b">
</head>
<body>
<header class="nav">
  <div class="wrap">
    <div class="brand"><a href="index.html" style="color:inherit">AVOUCH<span>.</span></a></div>
    <nav class="nav-links">
      <a href="index.html">홈</a>
      <a href="index.html#how">작동</a>
      <a href="index.html#econ">경제</a>
      <a href="index.html#status">상태</a>
    </nav>
  </div>
</header>
<main class="wrap">
<article class="doc">
{body}
</article>
</main>
<footer>
  <div class="wrap">
    <div class="backlink">마스터: ~/Documents/crypto/research/avouch-whitepaper-2026-10.md</div>
    <div><a href="index.html">← 홈</a></div>
  </div>
</footer>
</body>
</html>
"""
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(page):,} bytes)")


if __name__ == "__main__":
    if "--sync" in sys.argv:
        sync_from_wiki()
    build()

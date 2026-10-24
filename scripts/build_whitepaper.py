#!/usr/bin/env python3
"""Build the whitepaper pages (ko + en).

  uv run --with markdown python scripts/build_whitepaper.py            # 빌드만
  uv run --with markdown python scripts/build_whitepaper.py --sync     # 위키 마스터 -> content/ko -> 빌드

소스
  ko: 위키 마스터 ~/Documents/crypto/research/avouch-whitepaper-2026-10.md
      (--sync 가 content/ko/whitepaper.md 로 복사. 도커 이미지는 위키에 접근할 수 없다)
  en: 이 저장소에서 직접 관리하는 content/en/whitepaper.md (동기화 대상 아님)

출력
  site/ko/whitepaper.html, site/en/whitepaper.html
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
MASTER_KO = Path.home() / "Documents/crypto/research/avouch-whitepaper-2026-10.md"

WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

LANGS = {
    "ko": {
        "html_lang": "ko",
        "title": "Avouch 백서 — 보증이 편집권이 되는 게시 프로토콜",
        "src": ROOT / "content" / "ko" / "whitepaper.md",
        "out": ROOT / "site" / "ko" / "whitepaper.html",
        "nav": {"index.html": "홈", "index.html#how": "작동", "index.html#econ": "경제", "index.html#status": "상태"},
        "other": ("../en/", "EN"),
        "master_note": "마스터: ~/Documents/crypto/research/avouch-whitepaper-2026-10.md",
        "home": "← 홈",
    },
    "en": {
        "html_lang": "en",
        "title": "Avouch Whitepaper — a posting board where the bond is the editorial right",
        "src": ROOT / "content" / "en" / "whitepaper.md",
        "out": ROOT / "site" / "en" / "whitepaper.html",
        "nav": {"index.html": "Home", "index.html#how": "How", "index.html#econ": "Economics", "index.html#status": "Status"},
        "other": ("../ko/", "KO"),
        "master_note": "Korean master (longer, authoritative): avouch-whitepaper-2026-10.md",
        "home": "← Home",
    },
}

PAGE = """<!doctype html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="alternate" hreflang="ko" href="/ko/whitepaper.html">
<link rel="alternate" hreflang="en" href="/en/whitepaper.html">
<link rel="alternate" hreflang="x-default" href="/en/whitepaper.html">
<link rel="icon" href="../favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="../assets/style.css?v=20261024b">
</head>
<body>
<header class="nav">
  <div class="wrap">
    <div class="brand"><a href="index.html" style="color:inherit">AVOUCH<span>.</span></a></div>
    <nav class="nav-links">
{nav_links}
      <a href="{other_href}" onclick="document.cookie='lang={other_code};path=/;max-age=31536000';localStorage.setItem('avouch_lang','{other_code}')">{other_label}</a>
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
    <div class="backlink">{master_note}</div>
    <div><a href="index.html">{home}</a></div>
  </div>
</footer>
</body>
</html>
"""


def sync_from_wiki() -> None:
    if not MASTER_KO.exists():
        raise SystemExit(f"마스터를 찾을 수 없다: {MASTER_KO}")
    dest = LANGS["ko"]["src"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MASTER_KO, dest)
    print(f"synced {MASTER_KO.name} -> {dest.relative_to(ROOT)}")


def build(code: str) -> None:
    cfg = LANGS[code]
    src_path: Path = cfg["src"]
    if not src_path.exists():
        raise SystemExit(f"{src_path} 가 없다." + (" --sync 로 먼저 동기화해라." if code == "ko" else ""))

    src = src_path.read_text(encoding="utf-8")
    src = WIKILINK.sub(lambda m: Path(m.group(1)).stem, src)  # Obsidian links -> plain names
    body = markdown.markdown(src, extensions=["tables", "fenced_code", "sane_lists"], output_format="html5")

    other_href, other_label = cfg["other"]
    nav_links = "\n".join(f'      <a href="{href}">{label}</a>' for href, label in cfg["nav"].items())

    page = PAGE.format(
        html_lang=cfg["html_lang"],
        title=cfg["title"],
        nav_links=nav_links,
        other_href=other_href,
        other_label=other_label,
        other_code="en" if code == "ko" else "ko",
        body=body,
        master_note=cfg["master_note"],
        home=cfg["home"],
    )
    out: Path = cfg["out"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)} ({len(page):,} bytes)")


if __name__ == "__main__":
    if "--sync" in sys.argv:
        sync_from_wiki()
    for lang_code in LANGS:
        build(lang_code)

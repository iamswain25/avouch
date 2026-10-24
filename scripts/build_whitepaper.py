#!/usr/bin/env python3
"""Build the whitepaper pages (ko + en).

  uv run --with markdown python scripts/build_whitepaper.py            # 빌드만
  uv run --with markdown python scripts/build_whitepaper.py --sync     # 위키 마스터 -> content/ko -> 빌드

소스
  ko: 선택적 외부 마스터 (환경변수 AVOUCH_WIKI_MASTER). 미설정이면 content/ko/whitepaper.md 사용
  en: content/en/whitepaper.md (직접 관리)

출력
  site/ko/whitepaper.html, site/en/whitepaper.html
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
# optional: path to the Korean master outside the repo (env var). Unset -> content/ is authoritative.
MASTER_KO = Path(os.environ["AVOUCH_WIKI_MASTER"]) if os.environ.get("AVOUCH_WIKI_MASTER") else None

# local paths must not reach the public repo
SCRUB = [
    (re.compile(r"~/\S*?research/"), ""),
    (re.compile(r"~/\S*?/algorand/avouch-contracts"), "contracts"),
    (re.compile(r"~/\S*?/algorand/post-bond-escrow"), "contracts"),
    (re.compile(r"~/\S*?/avouch(?=\s|$|\))"), "."),
]


def scrub(text: str) -> str:
    for pattern, repl in SCRUB:
        text = pattern.sub(repl, text)
    return text

WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

LANGS = {
    "ko": {
        "html_lang": "ko",
        "lang_path": "ko",
        "title": "Avouch 백서 — 보증이 편집권이 되는 게시 프로토콜",
        "src": ROOT / "content" / "ko" / "whitepaper.md",
        "out": ROOT / "site" / "ko" / "whitepaper.html",
        "nav": {"./": "홈", "#how": "작동", "#econ": "경제", "#status": "상태"},
        "other": ("../en/", "EN"),
        "master_note": "한국어 정본: research/avouch-whitepaper-2026-10.md",
        "home": "← 홈",
    },
    "en": {
        "html_lang": "en",
        "lang_path": "en",
        "title": "Avouch Whitepaper — a posting board where the bond is the editorial right",
        "src": ROOT / "content" / "en" / "whitepaper.md",
        "out": ROOT / "site" / "en" / "whitepaper.html",
        "nav": {"./": "Home", "#how": "How", "#econ": "Economics", "#status": "Status"},
        "other": ("../ko/", "KO"),
        "master_note": "Korean master (longer, authoritative): research/avouch-whitepaper-2026-10.md",
        "home": "← Home",
    },
}

PAGE = """<!doctype html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="canonical" href="https://avouchprotocol.com/{lang_path}/whitepaper">
<meta property="og:type" content="article">
<link rel="alternate" hreflang="ko" href="/ko/whitepaper">
<link rel="alternate" hreflang="en" href="/en/whitepaper">
<link rel="alternate" hreflang="x-default" href="/en/whitepaper">
<link rel="icon" href="../favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="../assets/style.css?v=20261024b">
<script src="../assets/lang.js" defer></script>
</head>
<body>
<header class="nav">
  <div class="wrap">
    <div class="brand"><a href="./" style="color:inherit">AVOUCH<span>.</span></a></div>
    <nav class="nav-links">
{nav_links}
      <a href="{other_href}" data-lang="{other_code}">{other_label}</a>
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
    <div><a href="./">{home}</a></div>
  </div>
</footer>
</body>
</html>
"""


def sync_from_wiki() -> None:
    if MASTER_KO is None:
        print("AVOUCH_WIKI_MASTER 미설정 — content/ko/whitepaper.md 를 그대로 사용한다.")
        return
    if not MASTER_KO.exists():
        raise SystemExit(f"마스터를 찾을 수 없다: {MASTER_KO}")
    dest = LANGS["ko"]["src"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(scrub(MASTER_KO.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"synced {MASTER_KO.name} -> {dest.relative_to(ROOT)}")


def build(code: str) -> None:
    cfg = LANGS[code]
    src_path: Path = cfg["src"]
    if not src_path.exists():
        raise SystemExit(f"{src_path} 가 없다." + (" --sync 로 먼저 동기화해라." if code == "ko" else ""))

    src = scrub(src_path.read_text(encoding="utf-8"))
    src = WIKILINK.sub(lambda m: Path(m.group(1)).stem, src)  # Obsidian links -> plain names
    body = markdown.markdown(src, extensions=["tables", "fenced_code", "sane_lists"], output_format="html5")

    other_href, other_label = cfg["other"]
    nav_links = "\n".join(f'      <a href="{href}">{label}</a>' for href, label in cfg["nav"].items())

    page = PAGE.format(
        html_lang=cfg["html_lang"],
        lang_path=cfg["lang_path"],
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
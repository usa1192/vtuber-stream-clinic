import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


HTML_REPLACEMENTS = {
    "その配信、どこで伸びて<br>どこで離脱されてる？": "推しの配信、<br>見どころどこだった？",
    "トーク密度の参考点・OP終了・本編開始の推定・タイトル一致度・切り抜き候補": "OP終了・本編開始の推定・推しの見どころ候補・タイトル一致度",
    "Shorts候補の時間帯を提案": "推しを見返したい時間帯を提案",
    "動画時間と切り抜き候補に合わせて表示します。": "動画時間と見どころ候補に合わせて表示します。",
    "明るい区間はGeminiの切り抜き候補です。": "明るい区間はGeminiの見どころ候補です。",
    "切り抜き候補の時刻から仮の横軸": "見どころ候補の時刻から仮の横軸",
    "切り抜き候補がないため": "見どころ候補がないため",
    "<h3>Shorts・切り抜き候補</h3>": "<h3>推しの見どころ候補</h3>",
    "data.highlights.forEach(item => {": "data.highlights.forEach((item, index) => {",
    "row.querySelector('.tag').textContent = item.tag;": "row.querySelector('.tag').textContent = `${index < 3 ? '注目' : '追加'}・${item.tag}`;",
}


def write_public_index() -> None:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for old, new in HTML_REPLACEMENTS.items():
        html = html.replace(old, new)
    (DIST / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    write_public_index()

    api_base_url = os.getenv("KARTE_API_BASE_URL", "").rstrip("/")
    (DIST / "config.js").write_text(
        f'window.KARTE_API_BASE_URL = "{api_base_url}";\n',
        encoding="utf-8",
    )

    (DIST / "_headers").write_text(
        """/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

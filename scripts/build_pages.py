import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    shutil.copy2(ROOT / "index.html", DIST / "index.html")

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

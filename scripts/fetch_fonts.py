from pathlib import Path
from urllib.request import Request, urlopen

FONT_DIR = Path(__file__).resolve().parents[1] / "app" / "fonts"
FONT_DIR.mkdir(parents=True, exist_ok=True)
FONTS = {
    "NotoSans-Regular.ttf": "https://raw.githubusercontent.com/notofonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
    "NotoSans-Bold.ttf": "https://raw.githubusercontent.com/notofonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf",
    "NotoSansDevanagari-Regular.ttf": "https://raw.githubusercontent.com/notofonts/noto-fonts/main/hinted/ttf/NotoSansDevanagari/NotoSansDevanagari-Regular.ttf",
    "NotoSansTelugu-Regular.ttf": "https://raw.githubusercontent.com/notofonts/noto-fonts/main/unhinted/ttf/NotoSansTelugu/NotoSansTelugu-Regular.ttf",
}
for filename, url in FONTS.items():
    destination = FONT_DIR / filename
    if destination.exists() and destination.stat().st_size > 10_000:
        continue
    try:
        request = Request(url, headers={"User-Agent": "LumiDesk-AI-font-installer/1.0"})
        with urlopen(request, timeout=35) as response:
            data = response.read()
        if len(data) < 10_000:
            raise ValueError("downloaded file is unexpectedly small")
        destination.write_bytes(data)
        print(f"Installed {filename} ({len(data)} bytes)")
    except Exception as exc:
        print(f"Optional font {filename} could not be downloaded: {exc}")

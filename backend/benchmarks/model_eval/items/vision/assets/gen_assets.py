"""视觉种子题资产生成器（确定性绘制，可随时重建；PNG 与脚本一并入库）。

自造图完全贴自家使用面：文字识别（截图 OCR）、色块辨识、图表读取、计数。
社区图（MMMU/ChartQA 抽样）在 E6 鉴定波接入，与本批互补。

用法：backend/.venv/bin/python gen_assets.py   （在本目录执行）
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)     # Pillow ≥ 10
    except TypeError:                                # 老 Pillow 回退位图字体
        return ImageFont.load_default()


def word_number() -> None:
    im = Image.new("RGB", (640, 240), "white")
    d = ImageDraw.Draw(im)
    d.text((60, 70), "MANGO 42", fill="black", font=_font(80))
    im.save(HERE / "word_number.png")


def color_blocks() -> None:
    im = Image.new("RGB", (640, 360), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((20, 20, 420, 340), fill=(220, 30, 30))      # 大红块
    d.rectangle((440, 20, 560, 140), fill=(30, 60, 220))     # 小蓝块
    d.rectangle((440, 160, 560, 280), fill=(30, 170, 60))    # 小绿块
    im.save(HERE / "color_blocks.png")


def bar_chart() -> None:
    im = Image.new("RGB", (640, 400), "white")
    d = ImageDraw.Draw(im)
    bars = [("A", 120), ("B", 300), ("C", 200)]
    x = 80
    for label, h in bars:
        d.rectangle((x, 360 - h, x + 120, 360), fill=(90, 90, 200), outline="black")
        d.text((x + 45, 365), label, fill="black", font=_font(28))
        x += 180
    d.line((60, 360, 600, 360), fill="black", width=3)
    im.save(HERE / "bar_chart.png")


def log_screenshot() -> None:
    im = Image.new("RGB", (760, 220), (30, 30, 30))
    d = ImageDraw.Draw(im)
    lines = ["$ tail app.log", "INFO  server started on :14161",
             "ERROR disk full on /var/data", "INFO  cleanup done"]
    for i, ln in enumerate(lines):
        color = (255, 90, 90) if ln.startswith("ERROR") else (200, 230, 200)
        d.text((24, 20 + i * 46), ln, fill=color, font=_font(30))
    im.save(HERE / "log_screenshot.png")


def count_circles() -> None:
    im = Image.new("RGB", (640, 360), "white")
    d = ImageDraw.Draw(im)
    for cx, cy in [(110, 100), (300, 80), (500, 120), (200, 250), (430, 260)]:
        d.ellipse((cx - 45, cy - 45, cx + 45, cy + 45),
                  fill=(240, 160, 40), outline="black", width=3)
    im.save(HERE / "count_circles.png")


if __name__ == "__main__":
    for fn in (word_number, color_blocks, bar_chart, log_screenshot, count_circles):
        fn()
        print("✓", fn.__name__)

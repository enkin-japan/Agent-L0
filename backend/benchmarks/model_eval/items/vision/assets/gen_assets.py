"""视觉题资产生成器（确定性绘制，可随时重建；PNG 与脚本一并入库）。

两批：
- 种子批（word_number / color_blocks / bar_chart / log_screenshot / count_circles）：
  文字识别、色块辨识、图表读取、计数；
- App 面截图批（`app/*.png`，2026-08-24 加）：收据、设置面板、报错弹窗、登录表单、
  终端窗口、表格高亮格、通知横幅、文件列表、加粗词落在段落里、日历圈选、进度条、
  双栏比价卡——App 真正看见的是**截图**不是图表，这批补的就是这块空白。

确定性纪律：无随机、无时间戳、PNG 保存不带 pnginfo，同机重跑逐字节相同
（金测试 test_model_eval_text_vision_batch.py 钉这一条）。

用法：backend/.venv/bin/python gen_assets.py   （在本目录执行）
"""
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
APP_DIR = HERE / "app"

# 字体路径（macOS 系统自带）。缺失即回退位图字体——渲染会变，但同机同批仍确定性。
_UI = ("/System/Library/Fonts/Supplemental/Arial.ttf",
       "/System/Library/Fonts/Helvetica.ttc")
_UI_BOLD = ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Black.ttf")
_MONO = ("/System/Library/Fonts/Menlo.ttc",
         "/System/Library/Fonts/Supplemental/Courier New.ttf")
# CJK：找不到任一款时，题面渲染文字整体退回英文（见 _cjk 与 CJK_OK）
_CJK = ("/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc")


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)     # Pillow ≥ 10
    except TypeError:                                # 老 Pillow 回退位图字体
        return ImageFont.load_default()


def _first_font_path(candidates: Sequence[str]) -> Optional[str]:
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


CJK_OK = _first_font_path(_CJK) is not None

# 逐字节重建测试的前置声明：本机实际选中的字体路径集合。字体不同渲染必不同，
# 缺任一款时该测试应跳过而不是误报（gen_assets 的确定性口径本就是「同机同批」）。
REQUIRED_FONTS = [pth for pth in (_first_font_path(_UI), _first_font_path(_UI_BOLD),
                                  _first_font_path(_MONO), _first_font_path(_CJK)) if pth]


def _tt(candidates: Sequence[str], size: int):
    """按候选顺序取 TrueType 字体；全缺则回退位图字体（题仍可跑，只是不好看）。"""
    path = _first_font_path(candidates)
    if path is None:
        return _font(size)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return _font(size)


def ui(size: int):
    return _tt(_UI, size)


def ui_bold(size: int):
    return _tt(_UI_BOLD, size)


def mono(size: int):
    return _tt(_MONO, size)


def cjk(size: int):
    return _tt(_CJK, size)


def _cjk_text(zh: str, en: str) -> str:
    """CJK 字体缺席时整体退回英文文案——绝不画成豆腐块（豆腐块题=不可判分的坏题）。"""
    return zh if CJK_OK else en


def _save(im: Image.Image, path: Path) -> None:
    """确定性落盘：不写 pnginfo、不写 tIME，同输入逐字节相同。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG", optimize=True)


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




# ======================= App 面截图批（app/*.png） =======================
# 判分纪律：每张图的答案在图内**唯一**（无同值诱饵），字号一律 ≥ 18px。

_INK = (24, 24, 26)
_MUTED = (110, 112, 118)
_LINE = (206, 208, 214)
_BG = (247, 248, 250)
_ACCENT = (44, 110, 226)


def _panel(d: ImageDraw.ImageDraw, box, radius=12, fill="white", outline=_LINE, width=2):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _titlebar(d: ImageDraw.ImageDraw, box, title: str, font, radius=12):
    """macOS 风窗口标题栏（三个交通灯 + 居中标题）。"""
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0, y0, x1, y0 + 44), radius=radius, fill=(238, 239, 243),
                        outline=_LINE, width=2)
    d.rectangle((x0, y0 + 28, x1, y0 + 44), fill=(238, 239, 243))
    d.line((x0, y0 + 44, x1, y0 + 44), fill=_LINE, width=2)
    for i, color in enumerate(((255, 95, 87), (255, 189, 46), (40, 201, 64))):
        cx = x0 + 24 + i * 22
        d.ellipse((cx - 7, y0 + 15, cx + 7, y0 + 29), fill=color)
    d.text(((x0 + x1) // 2, y0 + 22), title, fill=_INK, font=font, anchor="mm")


def app_receipt(out: Path) -> None:
    """① 收据：4 条明细 + 小计/税 + TOTAL 21.06（图内唯一）。问 TOTAL。"""
    im = Image.new("RGB", (640, 760), _BG)
    d = ImageDraw.Draw(im)
    _panel(d, (30, 26, 610, 734), radius=10)
    f_shop, f_sub, f_row, f_tot = ui_bold(34), ui(20), ui(24), ui_bold(30)
    d.text((320, 74), _cjk_text("青山咖啡", "AOYAMA COFFEE"), fill=_INK, font=(
        cjk(34) if CJK_OK else f_shop), anchor="mm")
    d.text((320, 116), "AOYAMA COFFEE  ·  Shibuya", fill=_MUTED, font=f_sub, anchor="mm")
    d.text((320, 152), "Receipt  No. 5512", fill=_MUTED, font=f_sub, anchor="mm")
    d.line((70, 186, 570, 186), fill=_LINE, width=2)
    rows = [("Espresso", "4.50"), ("Bagel", "3.25"),
            ("Orange Juice", "5.00"), ("Fruit Salad", "6.75")]
    y = 222
    for name, price in rows:
        d.text((78, y), name, fill=_INK, font=f_row, anchor="lm")
        d.text((562, y), price, fill=_INK, font=f_row, anchor="rm")
        y += 58
    d.line((70, y - 12, 570, y - 12), fill=_LINE, width=2)
    d.text((78, y + 28), "Subtotal", fill=_MUTED, font=f_row, anchor="lm")
    d.text((562, y + 28), "19.50", fill=_MUTED, font=f_row, anchor="rm")
    d.text((78, y + 76), "Tax 8%", fill=_MUTED, font=f_row, anchor="lm")
    d.text((562, y + 76), "1.56", fill=_MUTED, font=f_row, anchor="rm")
    d.line((70, y + 108, 570, y + 108), fill=_INK, width=3)
    d.text((78, y + 152), "TOTAL", fill=_INK, font=f_tot, anchor="lm")
    d.text((562, y + 152), "21.06", fill=_INK, font=f_tot, anchor="rm")
    d.text((320, y + 216), "THANK YOU", fill=_MUTED, font=f_sub, anchor="mm")
    _save(im, out / "receipt.png")


def app_settings(out: Path) -> None:
    """② 设置面板：5 个开关，唯一 ON 的是 Dark mode。问哪个开关是开着的。"""
    im = Image.new("RGB", (720, 470), _BG)
    d = ImageDraw.Draw(im)
    _panel(d, (24, 20, 696, 450))
    _titlebar(d, (24, 20, 696, 450), _cjk_text("偏好设置", "Preferences"),
              cjk(22) if CJK_OK else ui_bold(22))
    f_label, f_state = ui(24), ui_bold(19)
    toggles = [("Auto-update", False), ("Dark mode", True),
               ("Share diagnostics", False), ("Beta channel", False),
               ("Sound effects", False)]
    y = 108
    for label, on in toggles:
        d.text((60, y), label, fill=_INK, font=f_label, anchor="lm")
        tx0, tx1 = 540, 604
        d.rounded_rectangle((tx0, y - 17, tx1, y + 17), radius=17,
                            fill=(52, 199, 89) if on else (198, 200, 206))
        knob = tx1 - 17 if on else tx0 + 17
        d.ellipse((knob - 14, y - 14, knob + 14, y + 14), fill="white")
        d.text((640, y), "ON" if on else "OFF",
               fill=(30, 140, 66) if on else _MUTED, font=f_state, anchor="lm")
        y += 62
    _save(im, out / "settings.png")


def app_error_dialog(out: Path) -> None:
    """③ 报错弹窗：错误码 E-4471（图内唯一形如错误码的串）。问错误码。"""
    im = Image.new("RGB", (700, 360), (232, 233, 238))
    d = ImageDraw.Draw(im)
    _panel(d, (40, 30, 660, 330), radius=14)
    d.ellipse((60, 60, 116, 116), fill=(228, 62, 62))
    d.text((88, 86), "!", fill="white", font=ui_bold(40), anchor="mm")
    d.text((140, 74), _cjk_text("同步失败", "Sync Failed"), fill=_INK,
           font=cjk(30) if CJK_OK else ui_bold(30), anchor="lm")
    d.text((140, 122), "The server could not be reached.", fill=_INK, font=ui(22), anchor="lm")
    d.text((140, 158), "Error code: E-4471", fill=_INK, font=ui_bold(24), anchor="lm")
    d.text((140, 196), "Please check your network and try again.", fill=_MUTED,
           font=ui(20), anchor="lm")
    d.rounded_rectangle((360, 244, 480, 300), radius=10, fill=(236, 237, 241),
                        outline=_LINE, width=2)
    d.text((420, 272), "Cancel", fill=_INK, font=ui(22), anchor="mm")
    d.rounded_rectangle((500, 244, 620, 300), radius=10, fill=_ACCENT)
    d.text((560, 272), "Retry", fill="white", font=ui_bold(22), anchor="mm")
    _save(im, out / "error_dialog.png")


def app_login_form(out: Path) -> None:
    """④ 登录表单：4 个带标签的输入框，第 2 个标签是 Email address。问第 2 个标签。"""
    im = Image.new("RGB", (660, 620), _BG)
    d = ImageDraw.Draw(im)
    _panel(d, (30, 24, 630, 596))
    d.text((330, 74), "Sign in to Northwind", fill=_INK, font=ui_bold(30), anchor="mm")
    f_lab, f_val = ui(20), ui(22)
    fields = [("Workspace URL", "acme.northwind.app"),
              ("Email address", "you@acme.com"),
              ("Password", "••••••••••"),
              ("Access token", "optional")]
    y = 128
    for label, placeholder in fields:
        d.text((70, y), label, fill=_MUTED, font=f_lab, anchor="lm")
        d.rounded_rectangle((70, y + 16, 590, y + 68), radius=8, fill="white",
                            outline=_LINE, width=2)
        d.text((86, y + 42), placeholder, fill=(160, 162, 170), font=f_val, anchor="lm")
        y += 92
    d.rounded_rectangle((70, y + 12, 590, y + 68), radius=10, fill=_ACCENT)
    d.text((330, y + 40), "Continue", fill="white", font=ui_bold(24), anchor="mm")
    _save(im, out / "login_form.png")


def app_terminal(out: Path) -> None:
    """⑤ 终端窗口：8 行带行号，唯一含 FAILED 的是第 5 行。问行号。"""
    im = Image.new("RGB", (840, 420), (18, 20, 24))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 840, 46), fill=(46, 48, 54))
    for i, color in enumerate(((255, 95, 87), (255, 189, 46), (40, 201, 64))):
        cx = 24 + i * 22
        d.ellipse((cx - 7, 16, cx + 7, 30), fill=color)
    d.text((420, 23), "bash — build.sh", fill=(214, 216, 222), font=ui(20), anchor="mm")
    lines = [
        ("resolving dependencies", (198, 204, 212)),
        ("compiling core module", (198, 204, 212)),
        ("compiling network module", (198, 204, 212)),
        ("running unit tests: 41 passed", (126, 214, 140)),
        ("integration test suite FAILED", (255, 108, 108)),
        ("skipping packaging step", (222, 200, 120)),
        ("cleaning temporary files", (198, 204, 212)),
        ("build finished with errors", (198, 204, 212)),
    ]
    f = mono(22)
    d.rectangle((0, 46, 62, 420), fill=(28, 30, 36))
    for i, (text, color) in enumerate(lines):
        y = 78 + i * 40
        d.text((44, y), str(i + 1), fill=(120, 124, 132), font=f, anchor="rm")
        d.text((80, y), text, fill=color, font=f, anchor="lm")
    _save(im, out / "terminal.png")


def app_table_cell(out: Path) -> None:
    """⑥ 表格截图 4 行 × 3 列，高亮格值 6,210（图内唯一）。问高亮格的值。"""
    im = Image.new("RGB", (760, 350), _BG)
    d = ImageDraw.Draw(im)
    x0, y0, w, h = 40, 34, 226, 68
    header = ("Region", "Units", "Revenue")
    rows = [("North", "128", "9,540"),
            ("South", "87", "6,210"),
            ("West", "203", "14,880")]
    f_h, f_c = ui_bold(24), ui(24)
    for c, text in enumerate(header):
        box = (x0 + c * w, y0, x0 + (c + 1) * w, y0 + h)
        d.rectangle(box, fill=(228, 230, 236), outline=_LINE, width=2)
        d.text(((box[0] + box[2]) // 2, y0 + h // 2), text, fill=_INK, font=f_h, anchor="mm")
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            box = (x0 + c * w, y0 + (r + 1) * h, x0 + (c + 1) * w, y0 + (r + 2) * h)
            hot = (r, c) == (1, 2)
            d.rectangle(box, fill=(255, 226, 92) if hot else "white",
                        outline=(214, 150, 20) if hot else _LINE, width=4 if hot else 2)
            d.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), text,
                   fill=_INK, font=f_c, anchor="mm")
    _save(im, out / "table_cell.png")


def app_notification(out: Path) -> None:
    """⑦ 通知横幅：右上角时间 09:45（图内唯一时间）。问时间。"""
    im = Image.new("RGB", (760, 230), (222, 224, 230))
    d = ImageDraw.Draw(im)
    _panel(d, (36, 40, 724, 190), radius=18, fill=(252, 252, 253))
    d.rounded_rectangle((66, 74, 138, 146), radius=16, fill=(226, 74, 74))
    d.text((102, 96), "MAR", fill="white", font=ui_bold(18), anchor="mm")
    d.text((102, 124), "19", fill="white", font=ui_bold(26), anchor="mm")
    f_app, f_title, f_sub = ui(20), ui_bold(26), ui(22)
    d.text((162, 82), _cjk_text("日历", "Calendar"), fill=_MUTED,
           font=cjk(20) if CJK_OK else f_app, anchor="lm")
    d.text((162, 122), _cjk_text("团队站会", "Team standup"), fill=_INK,
           font=cjk(26) if CJK_OK else f_title, anchor="lm")
    d.text((162, 158), _cjk_text("会议室 B", "Conference Room B"), fill=_MUTED,
           font=cjk(22) if CJK_OK else f_sub, anchor="lm")
    d.text((694, 82), "09:45", fill=_MUTED, font=ui(22), anchor="rm")
    _save(im, out / "notification.png")


def app_file_list(out: Path) -> None:
    """⑧ 文件列表：单位不同，最大的是 archive.zip（318 MB）。问最大文件的名字。"""
    im = Image.new("RGB", (700, 440), _BG)
    d = ImageDraw.Draw(im)
    _panel(d, (26, 22, 674, 418))
    _titlebar(d, (26, 22, 674, 418), "Documents", ui_bold(22))
    f_h, f_n, f_s = ui_bold(20), ui(23), ui(23)
    d.text((60, 92), "Name", fill=_MUTED, font=f_h, anchor="lm")
    d.text((640, 92), "Size", fill=_MUTED, font=f_h, anchor="rm")
    d.line((44, 112, 656, 112), fill=_LINE, width=2)
    files = [("notes.md", "12 KB"), ("budget.xlsx", "1.4 MB"),
             ("archive.zip", "318 MB"), ("photo.heic", "4.2 MB"),
             ("readme.txt", "2 KB")]
    y = 150
    for name, size in files:
        d.rounded_rectangle((58, y - 15, 82, y + 15), radius=4, fill=(196, 208, 232),
                            outline=(150, 166, 200), width=2)
        d.text((96, y), name, fill=_INK, font=f_n, anchor="lm")
        d.text((640, y), size, fill=_MUTED, font=f_s, anchor="rm")
        y += 56
    _save(im, out / "file_list.png")


def app_bold_word(out: Path) -> None:
    """⑨ 一段正文里恰好一个加粗词：quorum（正文其余处不出现）。问哪个词被加粗。"""
    im = Image.new("RGB", (800, 330), "white")
    d = ImageDraw.Draw(im)
    # 标题必须用常规字重：正文里的 quorum 是全图唯一的粗体，答案才唯一
    d.text((44, 44), "Board meeting notes", fill=_MUTED, font=ui(26), anchor="lm")
    d.line((44, 72, 756, 72), fill=_LINE, width=2)
    reg, bold = ui(24), ui_bold(24)
    paragraph = ("The committee met on Tuesday to review the annual plan. Because "
                 "only four of the nine members attended, the chair noted that no "
                 "quorum was present and deferred every binding vote to the next "
                 "session in April.")
    x, y, max_x = 44, 108, 756
    for word in paragraph.split(" "):
        font = bold if word.strip(".,") == "quorum" else reg
        w = d.textlength(word + " ", font=font)
        if x + w > max_x:
            x, y = 44, y + 40
        d.text((x, y), word, fill=_INK, font=font, anchor="la")
        x += w
    _save(im, out / "bold_word.png")


def app_calendar(out: Path) -> None:
    """⑩ 月历网格，19 号被红圈圈出（唯一圈选）。问被圈的日期。"""
    im = Image.new("RGB", (720, 600), "white")
    d = ImageDraw.Draw(im)
    d.text((360, 46), "March 2026", fill=_INK, font=ui_bold(32), anchor="mm")
    f_w, f_d = ui_bold(20), ui(24)
    cols = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    x0, y0, cw, ch = 40, 92, 92, 72
    for c, name in enumerate(cols):
        d.text((x0 + c * cw + cw // 2, y0 + 18), name, fill=_MUTED, font=f_w, anchor="mm")
    # 2026-03-01 是周日 → 首行只有最后一格有 1 号
    day = 1
    start_col = 6
    for r in range(6):
        for c in range(7):
            if r == 0 and c < start_col:
                continue
            if day > 31:
                break
            cx = x0 + c * cw + cw // 2
            cy = y0 + 60 + r * ch + ch // 2
            if day == 19:
                d.ellipse((cx - 30, cy - 30, cx + 30, cy + 30),
                          outline=(226, 46, 46), width=5)
            d.text((cx, cy), str(day), fill=_INK, font=f_d, anchor="mm")
            day += 1
    _save(im, out / "calendar.png")


def app_progress(out: Path) -> None:
    """⑪ 进度条 + 百分比标签 67%（图内唯一数字）。问百分比。"""
    im = Image.new("RGB", (700, 250), _BG)
    d = ImageDraw.Draw(im)
    _panel(d, (30, 26, 670, 224), radius=14)
    d.text((66, 78), "Uploading backup archive", fill=_INK, font=ui_bold(26), anchor="lm")
    bx0, bx1, by = 66, 634, 138
    d.rounded_rectangle((bx0, by, bx1, by + 30), radius=15, fill=(224, 226, 232))
    d.rounded_rectangle((bx0, by, bx0 + int((bx1 - bx0) * 0.67), by + 30),
                        radius=15, fill=_ACCENT)
    d.text((66, 190), "67%", fill=_INK, font=ui_bold(28), anchor="lm")
    d.text((634, 190), _cjk_text("请勿关闭窗口", "Do not close this window"),
           fill=_MUTED, font=cjk(20) if CJK_OK else ui(20), anchor="rm")
    _save(im, out / "progress.png")


def app_compare(out: Path) -> None:
    """⑫ 双栏比价卡：Standard $29 / Pro $79 → Pro 更贵。问哪一栏价格更高。"""
    im = Image.new("RGB", (760, 480), _BG)
    d = ImageDraw.Draw(im)
    f_name, f_price, f_unit, f_row = ui_bold(30), ui_bold(46), ui(20), ui(22)
    cards = [(40, "Standard", "$29", ("10 GB storage", "2 seats", "Email support")),
             (390, "Pro", "$79", ("200 GB storage", "10 seats", "Priority support"))]
    for x0, name, price, feats in cards:
        _panel(d, (x0, 30, x0 + 330, 450), radius=16)
        d.text((x0 + 165, 84), name, fill=_INK, font=f_name, anchor="mm")
        d.text((x0 + 165, 158), price, fill=_INK, font=f_price, anchor="mm")
        d.text((x0 + 165, 204), "per month", fill=_MUTED, font=f_unit, anchor="mm")
        d.line((x0 + 30, 236, x0 + 300, 236), fill=_LINE, width=2)
        for i, feat in enumerate(feats):
            d.text((x0 + 40, 278 + i * 46), "•", fill=_ACCENT, font=f_row, anchor="lm")
            d.text((x0 + 66, 278 + i * 46), feat, fill=_INK, font=f_row, anchor="lm")
    _save(im, out / "compare.png")


APP_GENERATORS = (app_receipt, app_settings, app_error_dialog, app_login_form,
                  app_terminal, app_table_cell, app_notification, app_file_list,
                  app_bold_word, app_calendar, app_progress, app_compare)


def generate_app_screens(out_dir: Path = APP_DIR) -> None:
    """生成 App 面截图批（确定性；同机重跑逐字节相同）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for fn in APP_GENERATORS:
        fn(out_dir)


if __name__ == "__main__":
    for fn in (word_number, color_blocks, bar_chart, log_screenshot, count_circles):
        fn()
        print("✓", fn.__name__)
    if not CJK_OK:
        print("! 未找到 CJK 字体，App 批渲染文字整体退回英文")
    generate_app_screens()
    print("✓ generate_app_screens →", APP_DIR)

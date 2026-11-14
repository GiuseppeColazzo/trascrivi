"""Anteprima delle bandiere di web/app.js, per guardarle senza browser.

Non è un renderer SVG: gestisce solo i pochi comandi che usiamo (rect, circle,
ellipse, path con M/L/H/V/Z e stroke). Serve a vedere se le bandiere escono
come devono, visto che in questa sessione il browser non parte.
"""
import re
from pathlib import Path

from PIL import Image, ImageDraw

SS = 16  # supersampling


def _nums(text):
    return [float(n) for n in re.findall(r"-?\d*\.?\d+", text)]


def _attrs(text):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', text))


def path_points(d):
    """Segmenti di un path fatto solo di M/L/H/V/Z (assoluti e relativi)."""
    tokens = re.findall(r"[MmLlHhVvZz]|-?\d*\.?\d+", d)
    segs, cur, start, cmd, i = [], (0.0, 0.0), None, None, 0
    while i < len(tokens):
        tok = tokens[i]
        if re.match(r"[A-Za-z]", tok):
            cmd = tok
            i += 1
            if cmd in "Zz" and start:
                cur = start
            continue
        if cmd in "Mm":
            x, y = float(tokens[i]), float(tokens[i + 1]); i += 2
            cur = (cur[0] + x, cur[1] + y) if cmd == "m" else (x, y)
            start = cur
        elif cmd in "Ll":
            x, y = float(tokens[i]), float(tokens[i + 1]); i += 2
            nxt = (cur[0] + x, cur[1] + y) if cmd == "l" else (x, y)
            segs.append((cur, nxt)); cur = nxt
        elif cmd in "Hh":
            x = float(tokens[i]); i += 1
            nxt = (cur[0] + x, cur[1]) if cmd == "h" else (x, cur[1])
            segs.append((cur, nxt)); cur = nxt
        elif cmd in "Vv":
            y = float(tokens[i]); i += 1
            nxt = (cur[0], cur[1] + y) if cmd == "v" else (cur[0], y)
            segs.append((cur, nxt)); cur = nxt
        else:  # comando non gestito: salta il numero
            i += 1
    return segs


def draw_flag(canvas, markup, ox, oy, scale, fg="#e9e7e4"):
    """Disegna il markup dentro il canvas, con l'origine in (ox, oy)."""
    d = ImageDraw.Draw(canvas, "RGBA")
    markup = markup.replace("currentColor", fg)

    def px(x, y):
        return (ox + x * scale, oy + y * scale)

    group_fill = None
    for match in re.finditer(r"<(/?)(\w+)((?:\s+[\w-]+=\"[^\"]*\")*)\s*(/?)>", markup):
        closing, tag, attr_text, _self = match.groups()
        if tag == "g":
            group_fill = _attrs(attr_text).get("fill") if not closing else None
            continue
        if closing:
            continue  # i tag di chiusura non disegnano niente
        a = _attrs(attr_text)
        fill = a.get("fill") or group_fill
        stroke = a.get("stroke")
        fill = None if fill in (None, "none") else fill
        stroke = None if stroke in (None, "none") else stroke
        width = float(a.get("stroke-width", 1)) * scale
        if tag == "rect":
            x, y = float(a.get("x", 0)), float(a.get("y", 0))
            w, h = float(a["width"]), float(a["height"])
            d.rectangle([px(x, y), px(x + w, y + h)], fill=fill,
                        outline=stroke, width=max(1, int(round(width))) if stroke else 0)
        elif tag in ("circle", "ellipse"):
            cx, cy = float(a.get("cx", 0)), float(a.get("cy", 0))
            rx = float(a.get("r", a.get("rx", 0)))
            ry = float(a.get("r", a.get("ry", 0)))
            d.ellipse([px(cx - rx, cy - ry), px(cx + rx, cy + ry)], fill=fill,
                      outline=stroke, width=max(1, int(round(width))) if stroke else 0)
        elif tag == "path":
            for (x1, y1), (x2, y2) in path_points(a.get("d", "")):
                d.line([px(x1, y1), px(x2, y2)], fill=stroke or fill,
                       width=max(1, int(round(width))))


src = Path("web/app.js").read_text(encoding="utf-8")
body = src[src.index("const FLAGS = {"):]
body = body[:body.index("\n};")]
flags = re.findall(r"^\s*(\w+):\s*`([^`]+)`", body, re.M)
print("bandiere trovate:", [name for name, _ in flags])

W, H = 21, 15
PAD, GAP = 18, 26
zoom = 8
cell = W * zoom * SS
row_h = H * zoom * SS
canvas = Image.new("RGB", (PAD * 2 + cell * len(flags) + GAP * (len(flags) - 1), row_h + PAD * 2), "#1b1e22")
for i, (name, markup) in enumerate(flags):
    ox = PAD + i * (cell + GAP)
    draw_flag(canvas, markup, ox, PAD, zoom * SS)
canvas = canvas.resize((canvas.width // SS, canvas.height // SS), Image.LANCZOS)

preview = Path(".smoke/flag-preview.png")
canvas.save(preview)
print("scritto", preview, canvas.size)

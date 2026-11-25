"""Generate crisp multi-resolution macOS (.icns) and Windows (.ico) app icons for ListenTo Studio."""

from pathlib import Path
from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent


def render_master_icon(size: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = Image.Draw.ImageDraw(img) if hasattr(Image, "Draw") else ImageDraw.Draw(img)

    margin = int(size * 0.06)
    box_size = size - 2 * margin
    radius = int(box_size * 0.225)

    # Rich studio gradient background (#0F172A -> #312E81 -> #4F46E5)
    grad = Image.new("RGBA", (box_size, box_size), (0, 0, 0, 0))
    g_px = grad.load()
    for y in range(box_size):
        t = y / max(1, box_size - 1)
        for x in range(box_size):
            tx = x / max(1, box_size - 1)
            mix = 0.65 * t + 0.35 * tx
            r = int(18 + (79 - 18) * mix)
            g = int(24 + (70 - 24) * mix)
            b = int(46 + (229 - 46) * mix)
            g_px[x, y] = (r, g, b, 255)

    mask = Image.new("L", (box_size, box_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, box_size - 1, box_size - 1), radius=radius, fill=255)
    img.paste(grad, (margin, margin), mask)

    # Subtle glowing inner border
    draw.rounded_rectangle(
        (margin + 3, margin + 3, size - margin - 4, size - margin - 4),
        radius=radius,
        outline=(129, 140, 248, 95),
        width=max(2, size // 128),
    )

    # Audio waveform bars (center-top)
    bars = [0.22, 0.42, 0.72, 0.95, 0.58, 0.82, 0.38, 0.20]
    bar_w = int(size * 0.052)
    gap = int(size * 0.032)
    total_w = len(bars) * bar_w + (len(bars) - 1) * gap
    start_x = (size - total_w) // 2
    center_y = int(size * 0.43)
    max_half_h = int(size * 0.21)

    for i, ratio in enumerate(bars):
        bx = start_x + i * (bar_w + gap)
        hh = max(bar_w // 2, int(max_half_h * ratio))
        color = (34, 211, 238, 255) if i in (2, 3, 5) else (241, 245, 249, 245)
        draw.rounded_rectangle(
            (bx, center_y - hh, bx + bar_w, center_y + hh),
            radius=bar_w // 2,
            fill=color,
        )

    # Bilingual subtitle tracks at bottom
    sub_w1 = int(size * 0.58)
    sub_h1 = int(size * 0.058)
    sub_y1 = int(size * 0.71)
    draw.rounded_rectangle(
        ((size - sub_w1) // 2, sub_y1, (size + sub_w1) // 2, sub_y1 + sub_h1),
        radius=sub_h1 // 2,
        fill=(255, 255, 255, 245),
    )

    sub_w2 = int(size * 0.42)
    sub_h2 = int(size * 0.042)
    sub_y2 = sub_y1 + sub_h1 + int(size * 0.032)
    draw.rounded_rectangle(
        ((size - sub_w2) // 2, sub_y2, (size + sub_w2) // 2, sub_y2 + sub_h2),
        radius=sub_h2 // 2,
        fill=(165, 180, 252, 215),
    )

    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master = render_master_icon(1024)
    png_path = OUT_DIR / "icon.png"
    ico_path = OUT_DIR / "icon.ico"
    icns_path = OUT_DIR / "icon.icns"

    master.save(png_path, format="PNG")
    master.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    master.save(icns_path, format="ICNS")
    print(f"Generated icons:\n  {png_path}\n  {ico_path}\n  {icns_path}")


if __name__ == "__main__":
    main()

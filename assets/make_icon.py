"""Draw the ReadyClip brand assets. Needs Pillow.

    readyclip.png / readyclip.ico   app icon: sunset-gradient tile, white
                                    viewfinder corners, play triangle
    banner.png                      README header, 1280x400
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
SIZE = 1024
TOP, BOTTOM = (255, 138, 30), (240, 54, 74)  # brand orange -> coral
INK = (27, 29, 34)                            # same as the app header
MUTED = (169, 173, 182)


def draw(size: int = SIZE) -> Image.Image:
    s = size / 1024
    gradient = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(gradient)
    for y in range(size):
        t = y / (size - 1)
        gd.line([(0, y), (size, y)],
                fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [40 * s, 40 * s, 984 * s, 984 * s], radius=220 * s, fill=255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(gradient, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    width = round(56 * s)
    lo, hi, arm = 230 * s, 794 * s, 150 * s
    for cx, cy, dx, dy in ((lo, lo, 1, 1), (hi, lo, -1, 1), (lo, hi, 1, -1), (hi, hi, -1, -1)):
        d.line([(cx, cy + dy * arm), (cx, cy), (cx + dx * arm, cy)],
               fill=white, width=width, joint="curve")
        r = width / 2
        for px, py in ((cx, cy + dy * arm), (cx + dx * arm, cy)):
            d.ellipse([px - r, py - r, px + r, py + r], fill=white)

    d.polygon([(432 * s, 352 * s), (432 * s, 672 * s), (690 * s, 512 * s)], fill=white)
    return img


def _font(name: str, size: int) -> ImageFont.ImageFont:
    for folder in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
        for candidate in (name, "DejaVuSans-Bold.ttf" if "b" in name else "DejaVuSans.ttf"):
            if (folder / candidate).is_file():
                return ImageFont.truetype(str(folder / candidate), size)
    return ImageFont.load_default()


def draw_banner(width: int = 1280, height: int = 400) -> Image.Image:
    k = 2  # draw at 2x, downsample for smooth edges
    W, H = width * k, height * k
    img = Image.new("RGB", (W, H), INK)

    # Warm glow behind the icon, echoing the icon's gradient.
    glow = Image.new("RGB", (W, H), INK)
    gd = ImageDraw.Draw(glow)
    cx, cy, r = 210 * k, H // 2, 260 * k
    gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(120, 52, 34))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(110 * k)), 0.9)

    # Thin brand stripe along the bottom.
    d = ImageDraw.Draw(img)
    for x in range(W):
        t = x / (W - 1)
        d.line([(x, H - 6 * k), (x, H)],
               fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))

    icon = draw(220 * k)
    img.paste(icon, (100 * k, (H - icon.height) // 2), icon)

    x = 370 * k
    d.text((x, 92 * k), "ReadyClip", font=_font("segoeuib.ttf", 92 * k), fill=(255, 255, 255))
    d.text((x, 212 * k), "Make action-cam videos play and share on any phone",
           font=_font("segoeui.ttf", 28 * k), fill=MUTED)

    chip_font = _font("segoeuib.ttf", 19 * k)
    cx = x
    for label in ("4K / 5.3K footage", "H.264 + HEVC", "NVIDIA GPU", "WhatsApp / Telegram"):
        tw = d.textlength(label, font=chip_font)
        box = [cx, 282 * k, cx + tw + 36 * k, 324 * k]
        d.rounded_rectangle(box, radius=21 * k, outline=(255, 120, 60), width=2 * k)
        d.text((cx + 18 * k, 289 * k), label, font=chip_font, fill=(255, 196, 160))
        cx = box[2] + 14 * k

    return img.resize((width, height), Image.LANCZOS)


def main() -> None:
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save(HERE / "readyclip.png")
    big.save(HERE / "readyclip.ico",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    draw_banner().save(HERE / "banner.png", optimize=True)
    for name in ("readyclip.png", "readyclip.ico", "banner.png"):
        print(f"wrote {HERE / name}")


if __name__ == "__main__":
    main()

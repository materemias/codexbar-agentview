# /// script
# dependencies = ["pillow"]
# ///
"""Turns a take into docs/<film>.mp4 and docs/<film>.gif, styled after Apple's product pages: a
white page with an eyebrow and a large headline over a light gray stage card holding the take's
midnight-titanium devices, each showing its recorded screen. A camera flies between framings: it
pulls back to frame where the action was and where it goes, then pushes in. Pointers, clicks and
pressed keys are drawn in.

    uv run film/compose.py <film> [WORK]   (WORK defaults to /tmp/codexbar-agentview-film-<film>)

The take (film/take_kde.py, film/take_tern.py) writes WORK/timeline.json:

    transitionMs  how long the camera takes between scenes
    link          the outro pill's text (default github.com/materemias/codexbar-agentview)
    devices       [{surface, kind: "display", size: [w, h], crop?: [x0, y0, x1, y1]}], left to right
    frames        {surface: [{t, path}]}, t in ms
    events        [{t, kind, ...}]:
                    scene    scene: a surface or "wide"; rect?: [x, y, w, h]; fill?
                    caption  eyebrow, headline
                    move     surface, x, y
                    click    surface
                    hide     surface (the pointer stays hidden until its next move)
                    key      keys: ["Super", "A"] or label: "harb"

Coordinates are shot pixels, before the crop. Needs ffmpeg, gifsicle optionally. Inter (SIL OFL)
is downloaded once into ~/.cache/codexbar-agentview-film.
"""

import io
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from multiprocessing import Pool
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

if len(sys.argv) < 2:
    sys.exit(__doc__)
FILM = sys.argv[1]
WORK = Path(sys.argv[2] if len(sys.argv) > 2 else f"/tmp/codexbar-agentview-film-{FILM}")
DOCS = Path(__file__).resolve().parent.parent / "docs"
CACHE = Path.home() / ".cache" / "codexbar-agentview-film"
INTER = "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip"
FPS = 30
W, H = 1600, 900
SS = 2  # device chrome and key caps are drawn this many times larger, then reduced, for smooth edges

WHITE = (255, 255, 255)
INK = (29, 29, 31)
CARD = (245, 245, 247)
BLUE = (0, 113, 227)
CARD_BOX = (60, 156, 1540, 840)
CARD_RADIUS = 36
INTRO_MS = 1100
TEXT_IN_MS = 520
TEXT_OUT_MS = 220
KEY_MS = 900


def fonts() -> dict[str, Path]:
    """Inter's display and text cuts, fetched once."""
    names = {"display": "InterDisplay-SemiBold.ttf", "text": "Inter-Medium.ttf", "text-semibold": "Inter-SemiBold.ttf"}
    CACHE.mkdir(parents=True, exist_ok=True)
    if not all((CACHE / n).exists() for n in names.values()):
        with urllib.request.urlopen(INTER, timeout=60) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        for n in names.values():
            (CACHE / n).write_bytes(archive.read(f"extras/ttf/{n}"))
    return {k: CACHE / n for k, n in names.items()}


FONT = fonts()


def font(kind: str, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT[kind]), round(size))


timeline = json.loads((WORK / "timeline.json").read_text())
frames, events = timeline["frames"], sorted(timeline["events"], key=lambda e: e["t"])
TRANSITION_MS = timeline["transitionMs"]
LINK = timeline.get("link", "github.com/materemias/codexbar-agentview")
scenes = [e for e in events if e["kind"] == "scene"]
captions = [e for e in events if e["kind"] == "caption"]
keys = [e for e in events if e["kind"] == "key"]


def ease(k: float) -> float:
    k = max(0.0, min(1.0, k))
    return 4 * k**3 if k < 0.5 else 1 - (-2 * k + 2) ** 3 / 2


def ease_out(k: float) -> float:
    k = max(0.0, min(1.0, k))
    return 1 - (1 - k) ** 3


def latest(items: list[dict], t: float, test=lambda item: True) -> dict | None:
    found = None
    for item in items:
        if item["t"] > t:
            break
        if test(item):
            found = item
    return found


def gradient(size: tuple[int, int], stops: list[tuple[float, tuple[int, ...]]], horizontal: bool = False) -> Image.Image:
    """A linear gradient through `stops` (position 0..1, RGB)."""
    length = size[0] if horizontal else size[1]
    line = Image.new("RGB", (length, 1))
    px = line.load()
    for i in range(length):
        p = i / max(1, length - 1)
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if a <= p <= b:
                k = (p - a) / (b - a) if b > a else 0
                px[i, 0] = tuple(round(x + (y - x) * k) for x, y in zip(ca, cb))
                break
    if horizontal:
        return line.resize(size)
    return line.transpose(Image.Transpose.ROTATE_90).transpose(Image.Transpose.FLIP_TOP_BOTTOM).resize(size)


def rounded_mask(size: tuple[int, int], radius: float, box=None) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box or (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


# Midnight titanium: a dark blue-gray metal with a lit top edge.
METAL = [(0, (92, 102, 128)), (0.06, (58, 66, 86)), (0.5, (40, 46, 62)), (1, (28, 32, 44))]
METAL_SIDE = [(0, (30, 34, 46)), (0.5, (64, 72, 94)), (1, (30, 34, 46))]


def metal(size, radius, stops=METAL, horizontal=False) -> Image.Image:
    image = gradient(size, stops, horizontal).convert("RGBA")
    image.putalpha(rounded_mask(size, radius))
    return image


def edge(draw: ImageDraw.ImageDraw, box, radius, color=(140, 150, 178, 150), width=2):
    draw.rounded_rectangle(box, radius=radius, outline=color, width=width)


class Device:
    """A display on a stand, drawn at native size, where a screen pixel is a shot pixel. It stands
    in the shared stage at world x, `height` world units tall; the floor is y = 0."""

    def __init__(self, spec: dict, x: float):
        if spec.get("kind", "display") != "display":
            raise ValueError(f"unknown device kind {spec['kind']!r}")
        self.surface = spec["surface"]
        self.crop = tuple(spec.get("crop") or (0, 0, *spec["size"]))
        sw, sh = self.crop[2] - self.crop[0], self.crop[3] - self.crop[1]
        # Chrome proportions of a 948-pixel-wide screen, scaled to this one.
        u = sw / 948
        bezel, frame = round(18 * u), round(10 * u)
        bw, bh = sw + 2 * (bezel + frame), sh + 2 * (bezel + frame)
        neck_h, foot_h = round(150 * u), round(20 * u)
        self.size = (bw, bh + neck_h + foot_h)
        self.screen = (frame + bezel, frame + bezel, sw, sh)
        self.screen_radius = round(4 * u)

        def draw(c: Image.Image, s: int):
            d = ImageDraw.Draw(c)
            neck_w, foot_w = round(190 * u) * s, round(430 * u) * s
            neck = metal((neck_w, (neck_h + round(30 * u)) * s), 6 * u * s, METAL_SIDE, horizontal=True)
            c.alpha_composite(neck, ((bw * s - neck_w) // 2, (bh - round(30 * u)) * s))
            foot = metal((foot_w, foot_h * s), 10 * u * s, [(0, (120, 130, 156)), (0.25, (62, 70, 92)), (1, (34, 38, 52))])
            c.alpha_composite(foot, ((bw * s - foot_w) // 2, (bh + neck_h) * s))
            c.alpha_composite(metal((bw * s, bh * s), 24 * u * s), (0, 0))
            edge(d, (s, s, (bw - 1) * s, (bh - 1) * s), 24 * u * s, width=max(2, round(2 * u)))
            d.rounded_rectangle((frame * s, frame * s, (bw - frame) * s, (bh - frame) * s), radius=16 * u * s, fill=(8, 8, 10, 255))

        self.draw = draw
        height = spec.get("height", 520)
        self.world = height / self.size[1]
        self.origin = (x, -height)  # world position of the native top left
        self.cache: tuple[str, Image.Image] | None = None
        self.shadow = self.make_shadow()

    def make_chrome(self) -> Image.Image:
        big = Image.new("RGBA", (self.size[0] * SS, self.size[1] * SS), (0, 0, 0, 0))
        self.draw(big, SS)
        return big.resize(self.size, Image.LANCZOS)

    def make_shadow(self) -> Image.Image:
        """A soft contact shadow on the stage floor, as wide as the stand's foot."""
        w = self.size[0]
        h = max(40, round(w * 0.09))
        shadow = Image.new("L", (round(w * 1.3), h * 2), 0)
        cx = shadow.width / 2
        ImageDraw.Draw(shadow).ellipse((cx - w * 0.31, h * 0.75, cx + w * 0.31, h * 1.25), fill=120)
        return shadow.filter(ImageFilter.GaussianBlur(h / 3))

    def to_world(self, x: float, y: float) -> tuple[float, float]:
        """A shot pixel's world position."""
        return (
            self.origin[0] + (self.screen[0] + x - self.crop[0]) * self.world,
            self.origin[1] + (self.screen[1] + y - self.crop[1]) * self.world,
        )

    def screen_world(self, rect=None) -> tuple[float, float, float, float]:
        x, y, w, h = rect or (self.crop[0], self.crop[1], self.screen[2], self.screen[3])
        wx, wy = self.to_world(x, y)
        return (wx, wy, w * self.world, h * self.world)

    def body_world(self) -> tuple[float, float, float, float]:
        return (self.origin[0], self.origin[1], self.size[0] * self.world, self.size[1] * self.world)

    def native(self, t: float) -> Image.Image:
        own = frames[self.surface]
        shot = latest(own, t) or own[0]
        file = shot.get("path") or shot["file"]
        if self.cache and self.cache[0] == file:
            return self.cache[1]
        if not hasattr(self, "chrome"):
            self.chrome = self.make_chrome()
            _, _, w, h = self.screen
            self.mask = rounded_mask((w, h), self.screen_radius)
            # A faint glass sheen across the upper left of the screen.
            sheen = Image.new("L", (w, h), 0)
            ImageDraw.Draw(sheen).polygon([(0, 0), (w * 0.55, 0), (w * 0.2, h), (0, h)], fill=10)
            self.sheen = sheen.filter(ImageFilter.GaussianBlur(w / 30))
        image = self.chrome.copy()
        x, y, w, h = self.screen
        screen = Image.open(file).convert("RGB").crop(self.crop)
        if screen.size != (w, h):
            screen = screen.resize((w, h), Image.LANCZOS)
        screen.paste((255, 255, 255), (0, 0), self.sheen)
        image.paste(screen, (x, y), self.mask)
        self.cache = (file, image)
        return image


devices: dict[str, Device] = {}
_x = 0.0
for _spec in timeline["devices"]:
    devices[_spec["surface"]] = _device = Device(_spec, _x)
    _x += _device.body_world()[2] + 70

card_w, card_h = CARD_BOX[2] - CARD_BOX[0], CARD_BOX[3] - CARD_BOX[1]


def framing(rect: tuple[float, float, float, float], fill: float, drop: float = 0) -> tuple[float, float, float]:
    """The camera (center x, center y, zoom) that shows world `rect` filling `fill` of the card,
    `drop` card pixels below its middle."""
    x, y, w, h = rect
    zoom = min(card_w * fill / w, card_h * fill / h)
    return (x + w / 2, y + h / 2 - drop / zoom, zoom)


def union(rects):
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return (x0, y0, x1 - x0, y1 - y0)


def on(scene: dict) -> list[Device]:
    return [*devices.values()] if scene["scene"] == "wide" else [devices[scene["scene"]]]


def shot_for(scene: dict) -> tuple[float, float, float]:
    """A scene frames all devices (`wide`), one device's screen, or a `rect` of that screen."""
    if scene["scene"] == "wide":
        # Lower than the middle, leaving room above for the outro's link and room below for shadows.
        return framing(union([d.body_world() for d in devices.values()]), scene.get("fill", 0.86), drop=24)
    device = devices[scene["scene"]]
    if "rect" in scene:
        return framing(device.screen_world(scene["rect"]), scene.get("fill", 0.92))
    return framing(device.screen_world(), scene.get("fill", 0.9))


def camera(t: float) -> tuple[float, float, float]:
    """Flies from the last shot to the current one: out to frame both, then in."""
    i = max(i for i, s in enumerate(scenes) if s["t"] <= t) if t >= scenes[0]["t"] else 0
    to = shot_for(scenes[i])
    if i == 0:
        k = ease_out((t - scenes[0]["t"]) / INTRO_MS)
        return (to[0], to[1], to[2] * (0.92 + 0.08 * k))
    a, b = scenes[i - 1], scenes[i]
    u = ease((t - b["t"]) / TRANSITION_MS)
    if u >= 1:
        return to
    fr = shot_for(a)
    la, lb = math.log(fr[2]), math.log(to[2])
    # Moving within one device is a straight push; between devices the camera pulls back first.
    bump = 0.0
    mid = to
    if on(a) != on(b) or a["scene"] == "wide" and b["scene"] == "wide":
        mid = framing(union([d.body_world() for s in (a, b) for d in on(s)]), 0.82)
        bump = max(0.0, (la + lb) / 2 - math.log(mid[2]))
    zoom = math.exp(la + (lb - la) * u - bump * math.sin(math.pi * u))
    lift = math.sin(math.pi * u) if bump > 0 else 0
    cx = fr[0] + (to[0] - fr[0]) * u + (mid[0] - (fr[0] + to[0]) / 2) * lift
    cy = fr[1] + (to[1] - fr[1]) * u + (mid[1] - (fr[1] + to[1]) / 2) * lift
    if bump == 0 and la != lb:
        # A straight push keeps the point both framings share still on the card: the camera
        # moves in step with the zoom, not linearly in world units.
        k = (zoom - fr[2]) / (to[2] - fr[2])
        cx = fr[0] + (to[0] - fr[0]) * k
        cy = fr[1] + (to[1] - fr[1]) * k
    return (cx, cy, zoom)


def to_card(cam, x, y) -> tuple[float, float]:
    cx, cy, z = cam
    return ((x - cx) * z + card_w / 2, (y - cy) * z + card_h / 2)


# A macOS-style pointer, tip at (0, 0).
ARROW = [(0, 0), (0, 21), (5, 16), (9, 24), (12.5, 22.5), (8.5, 15), (15, 15)]


def pointer(card: Image.Image, cam, device: Device, t: float):
    mine = lambda kind: lambda e: e["kind"] == kind and e.get("surface") == device.surface
    move = latest(events, t, mine("move"))
    if not move:
        return
    hidden = latest(events, t, mine("hide"))
    if hidden and hidden["t"] >= move["t"]:
        return
    draw = ImageDraw.Draw(card, "RGBA")
    px, py = to_card(cam, *device.to_world(move["x"], move["y"]))
    # Full size in a close-up, smaller as the camera pulls back.
    size = min(1.0, max(0.45, cam[2] / shot_for({"scene": device.surface})[2] * 0.7))
    click = latest(events, t, mine("click"))
    if click and t - click["t"] < 420:
        k = (t - click["t"]) / 420
        r = (6 + 16 * ease_out(k)) * size
        draw.ellipse((px - r, py - r, px + r, py + r), fill=(255, 255, 255, round(70 * (1 - k))), outline=(255, 255, 255, round(200 * (1 - k))), width=2)
    pts = [(px + x * size, py + y * size) for x, y in ARROW]
    draw.polygon(pts, fill=(0, 0, 0, 255), outline=(255, 255, 255, 255), width=2 if size > 0.7 else 1)


def render_card(t: float, cam, alpha: float) -> Image.Image:
    card = Image.new("RGBA", (card_w, card_h), (*CARD, 255))
    _, _, z = cam
    for device in devices.values():
        x, y, w, h = device.body_world()
        left, top = to_card(cam, x, y)
        ow, oh = w * z, h * z
        if left > card_w or top > card_h or left + ow < 0 or top + oh < 0:
            continue
        # The contact shadow under the stand's foot.
        sw, sh = device.shadow.size
        s_scale = ow / device.size[0]
        shadow = device.shadow.resize((max(1, round(sw * s_scale)), max(1, round(sh * s_scale))), Image.BILINEAR)
        shadow = shadow.point(lambda v: round(v * alpha))
        card.paste((0, 0, 0, 255), (round(left + ow / 2 - shadow.width / 2), round(top + oh - shadow.height / 2)), shadow)
        # Only the part of the device inside the card is scaled.
        native = device.native(t)
        k = device.size[0] / ow
        x0, y0 = max(0, -left), max(0, -top)
        x1, y1 = min(ow, card_w - left), min(oh, card_h - top)
        box = (x0 * k, y0 * k, x1 * k, y1 * k)
        size = (max(1, round(x1 - x0)), max(1, round(y1 - y0)))
        layer = native.resize(size, Image.LANCZOS, box=box, reducing_gap=2.0)
        if alpha < 1:
            layer.putalpha(layer.getchannel("A").point(lambda v: round(v * alpha)))
        card.alpha_composite(layer, (round(left + x0), round(top + y0)))
        pointer(card, cam, device, t)
    return card


PAGE = Image.new("RGB", (W, H), WHITE)
CARD_MASK = rounded_mask((card_w, card_h), CARD_RADIUS)
EYEBROW = font("display", 23)
HEADLINE = font("display", 66)
PILL_FONT = font("text", 19)
KEY_FONT = font("text-semibold", 22 * SS)
CAP_H = 46


def key_caps(labels: tuple[str, ...]) -> Image.Image:
    """Light key caps side by side, each with a soft drop shadow, as one RGBA image."""
    pad, gap, blur = 18, 10, 10
    widths = [max(CAP_H, round(KEY_FONT.getlength(label) / SS) + 2 * pad) for label in labels]
    w = sum(widths) + gap * (len(labels) - 1)
    margin = blur * 2
    big = Image.new("RGBA", ((w + 2 * margin) * SS, (CAP_H + 2 * margin) * SS), (0, 0, 0, 0))
    shadow = Image.new("L", big.size, 0)
    sd = ImageDraw.Draw(shadow)
    x = margin
    for cw in widths:
        sd.rounded_rectangle(((x) * SS, (margin + 5) * SS, (x + cw) * SS, (margin + CAP_H + 5) * SS), radius=11 * SS, fill=90)
        x += cw + gap
    big.paste((0, 0, 0, 255), (0, 0), shadow.filter(ImageFilter.GaussianBlur(blur * SS / 2)))
    d = ImageDraw.Draw(big)
    x = margin
    for label, cw in zip(labels, widths):
        box = (x * SS, margin * SS, (x + cw) * SS, (margin + CAP_H) * SS)
        d.rounded_rectangle(box, radius=11 * SS, fill=(252, 252, 253, 255), outline=(214, 214, 219, 255), width=SS)
        # The cap's lower lip.
        d.rounded_rectangle((box[0] + 2 * SS, box[3] - 5 * SS, box[2] - 2 * SS, box[3] - SS), radius=8 * SS, fill=(238, 238, 241, 255))
        d.text(((x + cw / 2) * SS, (margin + CAP_H / 2 - 1) * SS), label, font=KEY_FONT, fill=(*INK, 255), anchor="mm")
        x += cw + gap
    return big.resize((w + 2 * margin, CAP_H + 2 * margin), Image.LANCZOS)


CAPS: dict[tuple[str, ...], Image.Image] = {}


def pressed_keys(image: Image.Image, t: float):
    """The latest key press, bottom center of the card: it pops in, holds, then fades."""
    key = latest(keys, t)
    if not key or t - key["t"] >= KEY_MS:
        return
    labels = tuple(key.get("keys") or [key["label"]])
    if labels not in CAPS:
        CAPS[labels] = key_caps(labels)
    caps = CAPS[labels]
    k = t - key["t"]
    q = ease_out(k / 140) * (1 - ease((k - (KEY_MS - 260)) / 260))
    scale = 0.92 + 0.08 * ease_out(k / 140)
    layer = caps.resize((round(caps.width * scale), round(caps.height * scale)), Image.BICUBIC)
    layer.putalpha(layer.getchannel("A").point(lambda v: round(v * q)))
    y = CARD_BOX[3] - 44 - CAP_H / 2 + 6 * (1 - q)
    image.paste(layer, (round(W / 2 - layer.width / 2), round(y - layer.height / 2)), layer)


def text_layer(image: Image.Image, cap: dict, alpha: float, dy: float, eyebrow_alpha: float):
    d = ImageDraw.Draw(image, "RGBA")
    d.text((W / 2, 52), cap["eyebrow"], font=EYEBROW, fill=(*INK, round(255 * eyebrow_alpha)), anchor="mm")
    d.text((W / 2, 106 + dy), cap["headline"], font=HEADLINE, fill=(*INK, round(255 * alpha)), anchor="mm")


def headline(image: Image.Image, t: float):
    i = max((i for i, c in enumerate(captions) if c["t"] <= t), default=None)
    if i is None:
        return
    cap = captions[i]
    k = (t - cap["t"]) / TEXT_IN_MS
    prev = captions[i - 1] if i > 0 else None
    same_eyebrow = prev is not None and prev["eyebrow"] == cap["eyebrow"]
    out = (t - cap["t"]) / TEXT_OUT_MS
    if prev and out < 1:
        text_layer(image, prev, 1 - ease_out(out), -10 * ease_out(out), 1 if same_eyebrow else 1 - ease_out(out))
    delay = TEXT_OUT_MS * 0.6 / TEXT_IN_MS if prev else 0
    q = ease_out((k - delay) / (1 - delay)) if k > delay else 0
    if q > 0:
        text_layer(image, cap, q, 22 * (1 - q), 1 if same_eyebrow else q)


def outro_pill(image: Image.Image, t: float):
    last = scenes[-1]
    if last["scene"] != "wide" or len(scenes) < 2 or t < last["t"] + TRANSITION_MS * 0.7:
        return
    q = ease_out((t - last["t"] - TRANSITION_MS * 0.7) / 600)
    d = ImageDraw.Draw(image, "RGBA")
    w = d.textlength(LINK, font=PILL_FONT) + 56
    cy = CARD_BOX[1] + 46 + 10 * (1 - q)
    d.rounded_rectangle((W / 2 - w / 2, cy - 23, W / 2 + w / 2, cy + 23), radius=23, fill=(*BLUE, round(255 * q)))
    d.text((W / 2, cy), LINK, font=PILL_FONT, fill=(255, 255, 255, round(255 * q)), anchor="mm")


START = scenes[0]["t"]
END = timeline.get("end", max(f["t"] for fs in frames.values() for f in fs) + 400)
COUNT = int((END - START) / 1000 * FPS) + 1
OUT = WORK / "composed"


def render(n: int):
    t = START + n * 1000 / FPS
    image = PAGE.copy()
    cam = camera(t)
    alpha = ease_out((t - START) / INTRO_MS)
    card = render_card(t, cam, alpha)
    image.paste(card.convert("RGB"), CARD_BOX[:2], CARD_MASK)
    pressed_keys(image, t)
    headline(image, t)
    outro_pill(image, t)
    image.save(OUT / f"{n:05d}.png", compress_level=1)


if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    with Pool(max(1, (os.cpu_count() or 2) - 2)) as pool:
        # Neighbouring frames share shots, so each worker takes a run of them.
        pool.map(render, range(COUNT), chunksize=max(1, COUNT // ((os.cpu_count() or 2) * 3)))
    DOCS.mkdir(exist_ok=True)
    mp4, gif = DOCS / f"{FILM}.mp4", DOCS / f"{FILM}.gif"
    frames_in = ["-framerate", str(FPS), "-i", str(OUT / "%05d.png")]
    ffmpeg = ["ffmpeg", "-loglevel", "error", "-y"]
    subprocess.run(
        [*ffmpeg, *frames_in, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "slow", "-movflags", "+faststart", str(mp4)],
        check=True,
    )
    subprocess.run(
        [
            *ffmpeg,
            *frames_in,
            "-filter_complex",
            "[0]fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
            str(gif),
        ],
        check=True,
    )
    # The camera moves change every pixel, so the GIF is large; gifsicle, when installed, trims it.
    if shutil.which("gifsicle"):
        subprocess.run(["gifsicle", "-O3", "--lossy=40", "--batch", str(gif)], check=True)
    print(f"{COUNT} frames, {COUNT / FPS:.1f} s -> {gif}, {mp4}")

"""Selective recolors for the Prospector / Bartender sheets (build-time only)."""
import colorsys
from PIL import Image

# Regions are picked by hue/saturation in HSV. Values were tuned by eye against the
# Frontier 1864 Prospector sheet: shirt is the saturated red-orange, pants the dusty blue,
# hat the tan. Skin is a low-saturation peach and is left alone.
def _in(h, lo, hi):
    return lo <= h <= hi if lo <= hi else (h >= lo or h <= hi)

REGIONS = {
    "shirt": lambda h, s, v: _in(h, 0.955, 0.055) and s >= 0.40,
    "pants": lambda h, s, v: _in(h, 0.50, 0.72) and s >= 0.10,
    "hat":   lambda h, s, v: _in(h, 0.06, 0.15) and 0.14 <= s <= 0.42 and v <= 0.78,
}


def recolor(im: Image.Image, spec: dict, glitch: bool = False) -> Image.Image:
    """spec: region -> (hue, sat_mul, val_mul). hue None keeps hue."""
    im = im.convert("RGBA")
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            for name, test in REGIONS.items():
                if name in spec and test(hh, ss, vv):
                    nh, sm, vm = spec[name]
                    hh = hh if nh is None else nh
                    ss = max(0.0, min(1.0, ss * sm))
                    vv = max(0.0, min(1.0, vv * vm))
                    r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hh, ss, vv))
                    break
            if glitch:
                # cool everything toward cyan, scanlines every 3rd row
                r = int(r * 0.55)
                g = min(255, int(g * 0.9 + 30))
                b = min(255, int(b * 0.9 + 60))
                if y % 3 == 0:
                    a = int(a * 0.55)
            px[x, y] = (r, g, b, a)
    return im


VARIANTS = {
    # name: (base sheet, spec, glitch)
    "Outlaw":   {"shirt": (0.0, 0.0, 0.45), "hat": (0.0, 0.0, 0.35), "pants": (0.08, 0.6, 0.8)},
    "Rustler":  {"shirt": (0.12, 1.1, 1.05), "hat": (0.07, 1.2, 0.7), "pants": (0.08, 0.9, 0.7)},
    "Bandit":   {"shirt": (0.58, 0.9, 0.9), "hat": (0.0, 0.0, 0.25), "pants": (0.1, 0.5, 0.5)},
    "Reynolds": {"shirt": (0.98, 1.3, 0.55), "hat": (0.0, 0.0, 0.18), "pants": (0.0, 0.0, 0.25)},
    "Glitch":   {"shirt": (0.86, 1.6, 1.25), "hat": (0.0, 0.0, 0.3), "pants": (0.5, 1.4, 1.0)},
}

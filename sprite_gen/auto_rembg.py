"""Auto background removal via corner-color sampling + edge BFS.

Vectorized first pass uses numpy to mask all pixels close to the sampled
background color in one shot; the second pass walks edge-connected pixels with
a slightly looser threshold to clean residual halo. Pure-Python BFS is the same
shape as the vendored magenta cleanup but with the background color sampled
from the four image corners instead of hard-coded.

Tuning knobs (on the per-call API):
- threshold: how close to bg color to mark a pixel transparent in the strict pass.
- edge_threshold: looser distance used during edge BFS halo cleanup.

Produces RGBA images and a transparent GIF that survives palette rotation.
"""

from __future__ import annotations

import math
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image


def _sample_bg_color(img: Image.Image) -> tuple[int, int, int]:
    arr = np.asarray(img.convert("RGB"))
    h, w, _ = arr.shape
    pad = max(2, min(w, h) // 50)
    corners = np.concatenate(
        [
            arr[:pad, :pad].reshape(-1, 3),
            arr[:pad, -pad:].reshape(-1, 3),
            arr[-pad:, :pad].reshape(-1, 3),
            arr[-pad:, -pad:].reshape(-1, 3),
        ],
        axis=0,
    )
    median = np.median(corners, axis=0).astype(int)
    return int(median[0]), int(median[1]), int(median[2])


def auto_remove_bg(
    img: Image.Image,
    threshold: int = 80,
    edge_threshold: int = 120,
    chroma_key_color: tuple[int, int, int] | None = None,
) -> Image.Image:
    """Return an RGBA copy of `img` with the background removed.

    If `chroma_key_color` is provided, that exact color is used as the key
    (preferred when the prompt forces a known chroma green/magenta backdrop).
    Otherwise the background color is auto-sampled from the image corners.

    Two-pass: vectorized strict-distance kill + BFS halo cleanup from edges.
    """
    rgba = img.convert("RGBA").copy()
    arr = np.asarray(rgba).astype(np.int32)
    h, w, _ = arr.shape

    if chroma_key_color is not None:
        bg_r, bg_g, bg_b = chroma_key_color
    else:
        bg_r, bg_g, bg_b = _sample_bg_color(rgba)

    # Strict pass: any pixel close to bg becomes fully transparent.
    diff = arr[..., :3] - np.array([bg_r, bg_g, bg_b], dtype=np.int32)
    dist_sq = (diff * diff).sum(axis=-1)
    strict_mask = dist_sq < (threshold * threshold)
    arr_out = arr.copy()
    arr_out[strict_mask, 3] = 0
    arr_out[strict_mask, :3] = 0

    # Edge BFS halo cleanup: walk from every border pixel outward, kill pixels
    # whose color is still within edge_threshold of bg AND that are connected
    # via already-transparent pixels back to the edge.
    rgba_clean = Image.fromarray(arr_out.astype(np.uint8), mode="RGBA").copy()
    pixels = rgba_clean.load()

    visited = np.zeros((h, w), dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    for x in range(w):
        queue.append((x, 0))
        queue.append((x, h - 1))
    for y in range(h):
        queue.append((0, y))
        queue.append((w - 1, y))

    def _close_enough(r: int, g: int, b: int) -> bool:
        return math.sqrt((r - bg_r) ** 2 + (g - bg_g) ** 2 + (b - bg_b) ** 2) < edge_threshold

    while queue:
        x, y = queue.popleft()
        if x < 0 or x >= w or y < 0 or y >= h or visited[y, x]:
            continue
        visited[y, x] = True
        r, g, b, a = pixels[x, y]
        if a == 0:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and not visited[ny, nx]:
                        queue.append((nx, ny))
        elif _close_enough(r, g, b):
            pixels[x, y] = (0, 0, 0, 0)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and not visited[ny, nx]:
                        queue.append((nx, ny))

    return rgba_clean


def save_transparent_gif(
    frames: list[Image.Image], out_path: Path, duration: int = 150
) -> None:
    """GIF with one palette index reserved for transparency.

    Same approach as the vendored agent-sprite-forge encoder: stack frames,
    quantize together, then split back so all frames share a palette and the
    transparent index is consistent across the animation.
    """
    if not frames:
        raise ValueError("no frames to encode")

    key = (255, 0, 254)
    width, height = frames[0].size

    # Build the visible-pixel-only frames for saturation-boost analysis (so
    # the chroma key colour itself doesn't dominate the boost histogram).
    visible_rgbs: list[Image.Image] = []
    for f in frames:
        r, g, b, a = f.convert("RGBA").split()
        hard_mask = a.point(lambda v: 255 if v >= 128 else 0)
        rgb = Image.merge("RGB", (r, g, b))
        canvas = Image.new("RGB", (width, height), key)
        canvas.paste(rgb, (0, 0), hard_mask)
        visible_rgbs.append(canvas)

    from .postprocess import _saturation_boost_stack
    boost = _saturation_boost_stack(visible_rgbs)
    boost_rows = 1 if boost is not None else 0

    stacked = Image.new("RGB", (width, height * (len(frames) + boost_rows)), key)
    for i, canvas in enumerate(visible_rgbs):
        stacked.paste(canvas, (0, i * height))
    if boost is not None:
        stacked.paste(boost, (0, len(frames) * height))

    try:
        # libimagequant preserves rare saturated colors (purple eye glints)
        # much better than MEDIANCUT/ADAPTIVE.
        paletted = stacked.quantize(
            method=Image.Quantize.LIBIMAGEQUANT,
            colors=256, dither=Image.Dither.FLOYDSTEINBERG,
        )
    except (ValueError, OSError):
        # FASTOCTREE + saturation-boost tile preserves rare colors much
        # better than histogram-weighted MEDIANCUT. Skip dithering — it
        # dilutes 1–2 px highlights across neighbors before quantize.
        paletted = stacked.quantize(
            method=Image.Quantize.FASTOCTREE,
            colors=256, dither=Image.Dither.NONE,
        )
    palette = list(paletted.getpalette() or [])
    while len(palette) < 256 * 3:
        palette.append(0)

    key_index = None
    for i in range(256):
        if palette[i * 3 : i * 3 + 3] == list(key):
            key_index = i
            break
    if key_index is None:
        best = (None, 0)
        for i in range(256):
            r, g, b = palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2]
            d = (r - key[0]) ** 2 + (g - key[1]) ** 2 + (b - key[2]) ** 2
            if best[0] is None or d < best[0]:
                best = (d, i)
        key_index = best[1]

    if key_index != 0:
        lut = np.arange(256, dtype=np.uint8)
        lut[0], lut[key_index] = key_index, 0
        arr = np.array(paletted)
        arr = lut[arr]
        paletted = Image.fromarray(arr, mode="P")
        for ch in range(3):
            zero_idx = ch
            key_idx = key_index * 3 + ch
            palette[zero_idx], palette[key_idx] = palette[key_idx], palette[zero_idx]
        paletted.putpalette(palette)

    out_frames = [
        paletted.crop((0, i * height, width, (i + 1) * height)) for i in range(len(frames))
    ]
    out_frames[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=out_frames[1:],
        duration=duration,
        loop=0,
        disposal=2,
        transparency=0,
        background=0,
    )

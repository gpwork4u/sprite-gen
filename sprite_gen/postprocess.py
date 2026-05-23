"""Postprocess raw codex-generated sprite sheets.

Two slicing modes:
- chroma_key=True (magenta_grid template): remove #FF00FF background, trim,
  align, output transparent atlas + GIF.
- chroma_key=False (hd2d_anime_32frame and other reference-bg templates):
  keep the rendered background, slice the strip into equal cells.

In keep_bg mode, if `also_auto_rembg=True` we additionally run auto_rembg on
every frame and save a parallel `transparent/` bundle (frames + strip + GIF).

We never resample the codex-generated raw — the prompt is responsible for
producing an N:1 aspect ratio with equal-width panels, and we slice at the
native resolution by `actual_width // cols`. Resampling pixel art before
slicing was the wrong call: it degraded sprite quality without rescuing
panels that the model laid out at the wrong widths.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import _vendored_postprocess as vp
from .auto_rembg import auto_remove_bg, save_transparent_gif

CHROMA_GREEN: tuple[int, int, int] = (0, 177, 64)


@dataclass
class ProcessOptions:
    rows: int
    cols: int
    cell_size: int = 128
    chroma_key: bool = True

    # chroma_key=True only
    threshold: int = 100
    edge_threshold: int = 150
    fit_scale: float = 0.85
    trim_border: int = 4
    edge_clean_depth: int = 3
    align: str = "center"
    shared_scale: bool = True
    component_mode: str = "largest"
    component_padding: int = 0
    min_component_area: int = 1
    edge_touch_margin: int = 0

    # both modes
    duration_ms: int = 200
    label_prefix: str = "frame"

    # keep_bg only: also produce a parallel transparent/ bundle via chroma key.
    also_auto_rembg: bool = False
    rembg_threshold: int = 80
    rembg_edge_threshold: int = 120
    # When set, slicing finds panel boundaries by valleys in non-chroma column
    # density and rembg uses this exact color as the key. None = legacy behavior
    # (equal-share slicing, corner-sampled bg color).
    chroma_key_color: tuple[int, int, int] | None = CHROMA_GREEN

    # After auto_rembg, trim each frame to its alpha bbox and recenter on a
    # common fixed canvas. This is the last line of defense against the model
    # placing the character at slightly different x/y across panels: even if
    # slicing is off by 30px, recentering puts the character in the same spot
    # on every output frame so the GIF does not shake.
    recenter_transparent: bool = True
    recenter_align: str = "center"   # "center" or "bottom"
    recenter_padding: int = 8        # pixels of transparent margin on the canvas

    # Final post-processed frame size (square). When set, every output frame is
    # LANCZOS-resampled to (output_frame_size, output_frame_size) after recenter.
    # This is what UI's "frame_size_px" maps to — the actual size of files
    # users get back, independent of model-side generation dimensions.
    output_frame_size: int | None = None

    # Slicing strategy for chroma-keyed sheets. When True, each panel's crop is
    # expanded by `panel_overlap` ratio into its neighbors before chroma keying;
    # the largest connected component nearest the panel center is then chosen
    # as the character. This recovers characters whose limbs cross the panel
    # boundary, but assumes no characters overlap.
    component_slice: bool = True
    panel_overlap: float = 0.25

    # When LANCZOS-fitting frames into output_frame_size, the character is
    # scaled to occupy this ratio of the canvas (the rest is transparent
    # margin). Lower values leave more breathing room for hair tips, motion
    # lines, weapon trails, etc. that would otherwise crowd the canvas edge.
    output_fit_scale: float = 0.78

    # Eliminate "breathing" size jitter — when the model renders one panel at
    # 90% scale and the next at 110%, _resize_to_square's shared-scale path
    # preserves that drift on the final canvas. With this on, every frame is
    # rescaled INDIVIDUALLY so its character bbox occupies the same fraction
    # of the canvas, regardless of model-side scale drift between panels.
    normalize_per_frame_size: bool = True


@dataclass
class ProcessResult:
    output_dir: Path
    sheet: Path
    animation_gif: Path
    frame_paths: list[Path]
    edge_touch_frames: list[list[int]] = field(default_factory=list)
    meta_path: Path | None = None
    # filled when also_auto_rembg=True
    transparent_dir: Path | None = None
    transparent_strip: Path | None = None
    transparent_gif: Path | None = None
    transparent_frame_paths: list[Path] = field(default_factory=list)
    raw_size_actual: tuple[int, int] | None = None
    qc_warnings: list[str] = field(default_factory=list)
    qc_summary: dict = field(default_factory=dict)


def _find_chroma_cuts(
    raw: Image.Image,
    cols: int,
    chroma: tuple[int, int, int],
    threshold: int,
) -> tuple[list[int], bool]:
    """Find `cols+1` x-axis cut positions by locating chroma valleys near each
    expected boundary. Returns (cuts, used_content_aware).

    Strategy: build a per-column foreground-pixel count (pixels whose color is
    NOT close to the chroma key), then for each interior boundary i (1..cols-1)
    search a ±25% window around i*width/cols for the column with the lowest
    foreground count. This snaps cuts to the green gap between panels even when
    the model does not emit perfectly equal-width panels. Falls back to equal
    division if no clear valleys exist (e.g. chroma key not honored).
    """
    width, height = raw.size
    if cols <= 1:
        return [0, width], False

    arr = np.asarray(raw.convert("RGB")).astype(np.int32)
    diff = arr - np.array(chroma, dtype=np.int32)
    dist_sq = (diff * diff).sum(axis=-1)
    # Use a generous threshold: image_gen does not honor #00B140 exactly —
    # backgrounds typically come back at distance 80–140 from the target. The
    # absolute fg-count is irrelevant; what matters is the relative dip
    # between character-occupied columns and chroma-gap columns.
    is_fg = dist_sq >= (threshold * threshold)
    col_fg = is_fg.sum(axis=0).astype(np.float32)  # shape (W,)

    # Light smoothing so a thin character spike doesn't dominate valley search.
    kernel_w = max(3, width // 200)
    if kernel_w % 2 == 0:
        kernel_w += 1
    kernel = np.ones(kernel_w, dtype=np.float32) / kernel_w
    smoothed = np.convolve(col_fg, kernel, mode="same")

    # Relative valley threshold: a real panel gap drops well below the global
    # column-fg median. We accept a candidate cut when the smoothed value is
    # both <50% of the median AND below 30% of image height (sanity bound).
    median_fg = float(np.median(smoothed))
    valley_ceiling = max(min(0.5 * median_fg, 0.30 * height), 0.05 * height)
    expected = width / cols
    search_half = expected * 0.30
    cuts = [0]
    used_content_aware = True
    for i in range(1, cols):
        center = int(round(i * expected))
        lo = max(1, int(center - search_half))
        hi = min(width - 1, int(center + search_half))
        if hi <= lo:
            cuts.append(center)
            used_content_aware = False
            continue
        local_min = lo + int(np.argmin(smoothed[lo:hi]))
        if smoothed[local_min] > valley_ceiling:
            # No clean valley in window; the model probably did not paint
            # chroma between panels here. Fall back to equal split for THIS cut.
            cuts.append(center)
            used_content_aware = False
        else:
            cuts.append(local_min)
    cuts.append(width)
    return cuts, used_content_aware


def _slice_keep_bg(
    raw: Image.Image,
    rows: int,
    cols: int,
    chroma: tuple[int, int, int] | None,
    threshold: int,
) -> tuple[list[Image.Image], list[dict]]:
    width, height = raw.size
    cell_h = height // rows
    if chroma is not None:
        x_cuts, content_aware = _find_chroma_cuts(raw, cols, chroma, threshold)
    else:
        x_cuts = [c * (width // cols) for c in range(cols)] + [width]
        content_aware = False

    frames: list[Image.Image] = []
    qc: list[dict] = []
    for r in range(rows):
        for c in range(cols):
            box = (x_cuts[c], r * cell_h, x_cuts[c + 1], (r + 1) * cell_h)
            frames.append(raw.crop(box))
            qc.append({
                "grid": [r, c],
                "source_box": list(box),
                "content_aware_slice": content_aware,
            })
    return frames, qc


def _find_cuts_via_peaks(
    cleaned_full: Image.Image, cols: int
) -> list[int] | None:
    """Fallback for when characters are packed too close to leave clean green
    gaps. Use the cleaned (alpha) image: column-wise alpha density should have
    `cols` peaks (one per character). Place cuts at the local minimum density
    between adjacent peaks.

    Returns None if we cannot find `cols` distinct peaks.
    """
    arr = np.asarray(cleaned_full).astype(np.int32)
    if arr.ndim != 3 or arr.shape[2] < 4:
        return None
    alpha = arr[..., 3]
    col_density = (alpha > 0).sum(axis=0).astype(np.float32)
    width = col_density.shape[0]
    if width == 0 or col_density.max() == 0:
        return None

    # Smooth aggressively to merge a character's body into one peak even when
    # silhouette has gaps (e.g. between legs).
    kernel = max(5, width // 50)
    if kernel % 2 == 0:
        kernel += 1
    smoothed = np.convolve(col_density, np.ones(kernel) / kernel, mode="same")

    # Greedy peak picker with minimum spacing: pick the highest-density column,
    # zero out a window of (width / cols) around it, repeat until we have
    # `cols` peaks (or run out of mass).
    work = smoothed.copy()
    expected = width // cols
    half = max(1, int(expected * 0.4))
    peaks: list[int] = []
    for _ in range(cols):
        i = int(np.argmax(work))
        if work[i] <= 0:
            break
        peaks.append(i)
        lo, hi = max(0, i - half), min(width, i + half)
        work[lo:hi] = 0
    if len(peaks) < cols:
        return None
    peaks.sort()
    cuts = [0]
    for a, b in zip(peaks, peaks[1:]):
        valley = a + int(np.argmin(smoothed[a:b]))
        cuts.append(valley)
    cuts.append(width)
    return cuts


def _summarize_qc(qc: list[dict]) -> dict:
    """Roll up per-panel QC into a batch-level summary with warnings.

    Modeled on agent-sprite-forge's edge_touch_frames + adds size-consistency
    detection: if one frame's character bbox is much smaller/bigger than the
    median, the character was probably partly clipped or the wrong component
    was selected.
    """
    panels_with_data = [p for p in qc if "component_bbox_size" in p]
    if not panels_with_data:
        return {"warnings": [], "edge_touch_panels": [], "size_outlier_panels": []}

    widths = [p["component_bbox_size"][0] for p in panels_with_data]
    heights = [p["component_bbox_size"][1] for p in panels_with_data]
    med_w = sorted(widths)[len(widths) // 2]
    med_h = sorted(heights)[len(heights) // 2]

    edge_touch_panels: list[int] = []
    size_outliers: list[int] = []
    warnings: list[str] = []
    for p in panels_with_data:
        if p.get("clipped_at_image_edge") or p.get("clipped_at_internal_cut"):
            edge_touch_panels.append(p["panel"])
            sides = [s for s, k in (
                ("left", "touch_left"), ("right", "touch_right"),
                ("top", "touch_top"), ("bottom", "touch_bottom"),
            ) if p.get(k)]
            note = f"panel {p['panel']}: character clipped at {','.join(sides)}"
            if p.get("clipped_at_image_edge"):
                note += " (touches image boundary — unrecoverable; regenerate)"
            elif p.get("clipped_at_internal_cut"):
                note += " (touches internal cut — try increasing panel_overlap)"
            warnings.append(note)
        w, h = p["component_bbox_size"]
        if med_w > 0 and abs(w - med_w) / med_w > 0.20:
            size_outliers.append(p["panel"])
            warnings.append(
                f"panel {p['panel']}: bbox width {w}px deviates >20% from median {med_w}px — "
                "likely a clipped frame or wrong component selected"
            )
        elif med_h > 0 and abs(h - med_h) / med_h > 0.20:
            size_outliers.append(p["panel"])
            warnings.append(
                f"panel {p['panel']}: bbox height {h}px deviates >20% from median {med_h}px"
            )
    return {
        "warnings": warnings,
        "edge_touch_panels": sorted(set(edge_touch_panels)),
        "size_outlier_panels": sorted(set(size_outliers)),
        "median_bbox_size": [med_w, med_h],
    }


def _label_components_4conn(
    alpha_arr: np.ndarray, min_area: int = 8,
) -> tuple[np.ndarray, list[dict]]:
    """4-connectivity component labeling. Returns (labels[H,W], components).

    Each component dict carries label, area, bbox. Pixels in components below
    `min_area` keep their labels (so callers can still mask them) but are
    excluded from the returned components list — those small labels are
    treated as "non-owned" (painted as background) by the slicer, matching
    the spec dust-removal behaviour.
    """
    h, w = alpha_arr.shape
    labels = np.zeros((h, w), dtype=np.int32)
    components: list[dict] = []
    cur = 0
    alpha_pos = alpha_arr > 0
    for y0 in range(h):
        for x0 in range(w):
            if not alpha_pos[y0, x0] or labels[y0, x0]:
                continue
            cur += 1
            stack = [(x0, y0)]
            labels[y0, x0] = cur
            area = 0
            min_x = max_x = x0
            min_y = max_y = y0
            while stack:
                cx, cy = stack.pop()
                area += 1
                if cx < min_x:
                    min_x = cx
                elif cx > max_x:
                    max_x = cx
                if cy < min_y:
                    min_y = cy
                elif cy > max_y:
                    max_y = cy
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and alpha_pos[ny, nx] and not labels[ny, nx]:
                        labels[ny, nx] = cur
                        stack.append((nx, ny))
            if area >= min_area:
                components.append({
                    "label": cur,
                    "area": area,
                    "bbox": (min_x, min_y, max_x + 1, max_y + 1),
                })
    components.sort(key=lambda c: c["area"], reverse=True)
    return labels, components


def _slice_with_components(
    raw: Image.Image,
    cleaned_full: Image.Image,
    cols: int,
    x_cuts: list[int],
    overlap_ratio: float,
    chroma_key_color: tuple[int, int, int] | None = None,
) -> tuple[list[Image.Image], list[Image.Image], list[dict]]:
    """For each panel, expand its crop into both neighbors by `overlap_ratio`,
    pick the largest connected character component closest to the panel center,
    and crop both the with-bg (raw) and transparent (cleaned) panels to that
    component's bbox.

    Returns (raw_frames, transparent_frames, qc).
    """
    width, height = cleaned_full.size
    raw_frames: list[Image.Image] = []
    transp_frames: list[Image.Image] = []
    qc: list[dict] = []
    chroma_rgb_arr = (
        np.array(chroma_key_color, dtype=np.uint8)
        if chroma_key_color is not None else None
    )
    for c in range(cols):
        panel_l, panel_r = x_cuts[c], x_cuts[c + 1]
        panel_w = panel_r - panel_l
        panel_cx = (panel_l + panel_r) / 2.0
        overlap = int(panel_w * overlap_ratio)
        ex_l = max(0, panel_l - overlap)
        ex_r = min(width, panel_r + overlap)

        cleaned_panel = cleaned_full.crop((ex_l, 0, ex_r, height))
        raw_panel = raw.crop((ex_l, 0, ex_r, height))

        # 4-connectivity component labelling. Keeping the labels array (not
        # just the components list) lets us mask neighbour-character pixels
        # out of the with-bg crop pixel-perfectly — bbox-based masking would
        # over-paint when neighbouring characters' bboxes overlap our owned
        # character's bbox. Without this, characters drawn close to the panel
        # edge would leak into the next/previous frame's with-bg crop.
        cleaned_panel_arr = np.array(cleaned_panel)  # H,W,4
        labels_panel, components = _label_components_4conn(
            cleaned_panel_arr[..., 3], min_area=8,
        )
        owned: list[dict] = []
        for comp in components:
            bx0c, _, bx1c, _ = comp["bbox"]
            comp_cx_raw = ex_l + (bx0c + bx1c) / 2.0
            # ownership: distance from THIS panel's center vs nearest neighbor center
            this_dist = abs(comp_cx_raw - panel_cx)
            owned_by_this = True
            for cc in range(cols):
                if cc == c:
                    continue
                nb_cx = (x_cuts[cc] + x_cuts[cc + 1]) / 2.0
                if abs(comp_cx_raw - nb_cx) < this_dist:
                    owned_by_this = False
                    break
            if owned_by_this:
                owned.append(comp)

        if not owned:
            # Fallback: just use the panel's own (non-expanded) crop.
            t = cleaned_full.crop((panel_l, 0, panel_r, height))
            r = raw.crop((panel_l, 0, panel_r, height))
            transp_frames.append(t)
            raw_frames.append(r)
            qc.append({"panel": c, "fallback": "no_components"})
            continue

        # Union bbox over all owned components.
        bx0 = min(comp["bbox"][0] for comp in owned)
        by0 = min(comp["bbox"][1] for comp in owned)
        bx1 = max(comp["bbox"][2] for comp in owned)
        by1 = max(comp["bbox"][3] for comp in owned)
        # Padding so faint anti-aliased extremities (hair tips, motion line
        # tails, weapon glows whose alpha fades below the connected-component
        # threshold) survive the bbox crop.
        pad = 14
        bx0 = max(0, bx0 - pad)
        by0 = max(0, by0 - pad)
        bx1 = min(cleaned_panel.size[0], bx1 + pad)
        by1 = min(cleaned_panel.size[1], by1 + pad)
        chosen = max(owned, key=lambda c: c["area"])  # for QC reporting
        # bbox is in expanded-panel local coords. Crop both with-bg and cleaned
        # at the same window so they align pixel-for-pixel.
        cleaned_crop = cleaned_panel.crop((bx0, by0, bx1, by1))
        raw_crop = raw_panel.crop((bx0, by0, bx1, by1))

        # Mask out neighbour-character pixels that fall inside our crop
        # rectangle. Without this, a neighbour character drawn close to the
        # panel boundary leaks into our crop's left/right edge as the rectangle
        # spans across the panel cut. We use the per-pixel labels (not bboxes)
        # so this works even when bboxes overlap.
        owned_label_set = {comp["label"] for comp in owned}
        labels_crop = labels_panel[by0:by1, bx0:bx1]
        not_owned_mask = (labels_crop > 0) & ~np.isin(labels_crop, list(owned_label_set))
        if not_owned_mask.any():
            cleaned_arr = np.array(cleaned_crop)
            cleaned_arr[not_owned_mask] = (0, 0, 0, 0)
            cleaned_crop = Image.fromarray(cleaned_arr, "RGBA")
            if chroma_rgb_arr is not None:
                raw_arr = np.array(raw_crop.convert("RGB"))
                raw_arr[not_owned_mask] = chroma_rgb_arr
                raw_crop = Image.fromarray(raw_arr, "RGB").convert("RGBA")
        transp_frames.append(cleaned_crop)
        raw_frames.append(raw_crop)

        ex_w, ex_h = cleaned_panel.size
        # Edge-touch flags. Touching the expanded crop edge means the character
        # was clipped at that side and our overlap was not enough — visible
        # cropping in the final frame. We separately note whether that edge
        # coincides with the actual image boundary (unrecoverable: model never
        # painted those pixels) versus an internal cut (could expand further).
        touch_left = bx0 <= 0
        touch_right = bx1 >= ex_w
        touch_top = by0 <= 0
        touch_bottom = by1 >= ex_h
        at_image_left = touch_left and ex_l <= 0
        at_image_right = touch_right and ex_r >= width
        clipped_at_image_edge = at_image_left or at_image_right or touch_top or touch_bottom
        clipped_at_internal_cut = (touch_left and not at_image_left) or (touch_right and not at_image_right)

        qc.append({
            "panel": c,
            "panel_bounds_raw": [panel_l, panel_r],
            "expanded_bounds_raw": [ex_l, ex_r],
            "component_bbox_local": [bx0, by0, bx1, by1],
            "component_bbox_size": [bx1 - bx0, by1 - by0],
            "component_area": int(chosen["area"]),
            "component_count": len(components),
            "owned_component_count": len(owned),
            "touch_left": touch_left,
            "touch_right": touch_right,
            "touch_top": touch_top,
            "touch_bottom": touch_bottom,
            "clipped_at_image_edge": clipped_at_image_edge,
            "clipped_at_internal_cut": clipped_at_internal_cut,
        })
    return raw_frames, transp_frames, qc


def _normalize_keep_bg_frames(
    frames: list[Image.Image],
    chroma: tuple[int, int, int],
) -> list[Image.Image]:
    """Pad every kept-bg cell with chroma fill to the largest cell's dimensions
    so the strip and per-frame outputs share a uniform canvas. The model often
    drifts the panel widths (rightmost panel narrower than the rest); without
    this, the strip looks misaligned even though slicing landed on real green
    valleys.
    """
    if not frames:
        return frames
    max_w = max(f.size[0] for f in frames)
    max_h = max(f.size[1] for f in frames)
    out: list[Image.Image] = []
    fill = (chroma[0], chroma[1], chroma[2], 255)
    for f in frames:
        rgba = f.convert("RGBA")
        canvas = Image.new("RGBA", (max_w, max_h), fill)
        x = (max_w - rgba.size[0]) // 2
        y = (max_h - rgba.size[1]) // 2
        canvas.paste(rgba, (x, y))
        out.append(canvas)
    return out


def _recenter_transparent_frames(
    frames: list[Image.Image],
    align: str,
    padding: int,
) -> list[Image.Image]:
    """Trim each frame to its alpha bbox and place all frames on a common
    canvas at consistent x/y so the character does not jitter across the GIF.

    No resampling — pixel-perfect. The common canvas size is
    (max_bbox_w + 2*padding, max_bbox_h + 2*padding) and every frame is pasted
    centered horizontally; vertically, "center" centers the bbox while
    "bottom" anchors the bbox bottom (good for walk/run, prevents head-bob
    from shifting the whole character).
    """
    if not frames:
        return frames
    cropped: list[tuple[Image.Image, tuple[int, int, int, int] | None]] = []
    for f in frames:
        rgba = f.convert("RGBA")
        bbox = rgba.getbbox()
        if bbox is None:
            cropped.append((rgba, None))
        else:
            cropped.append((rgba.crop(bbox), bbox))

    valid = [c for c, _ in cropped if c.size[0] > 0 and c.size[1] > 0]
    if not valid:
        return frames
    max_w = max(c.size[0] for c in valid)
    max_h = max(c.size[1] for c in valid)
    canvas_w = max_w + 2 * padding
    canvas_h = max_h + 2 * padding

    out: list[Image.Image] = []
    for img, _ in cropped:
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        w, h = img.size
        if w > 0 and h > 0:
            paste_x = (canvas_w - w) // 2
            if align == "bottom":
                paste_y = canvas_h - h - padding
            else:
                paste_y = (canvas_h - h) // 2
            canvas.paste(img, (paste_x, paste_y), img)
        out.append(canvas)
    return out


def _resize_to_square(
    frames: list[Image.Image],
    size: int,
    bg_fill: tuple[int, int, int, int] = (0, 0, 0, 0),
    fit_scale: float = 0.85,
) -> list[Image.Image]:
    """LANCZOS-resize each frame so its longer side fits `size * fit_scale`
    (leaving margin around the character), then center on a size×size canvas
    filled with `bg_fill`. Preserves aspect ratio. fit_scale<1 prevents the
    character from touching the canvas edges, which the user perceives as
    clipping even when the bbox is fully captured.

    Shared scale across all frames: we use the global max(w,h) over the batch
    so every frame uses the SAME scale factor — character size stays constant,
    only pose changes.
    """
    if not frames:
        return frames
    target = max(1, int(round(size * fit_scale)))
    valid_dims = [(f.size[0], f.size[1]) for f in frames if f.size[0] > 0 and f.size[1] > 0]
    if not valid_dims:
        return [Image.new("RGBA", (size, size), bg_fill) for _ in frames]
    global_max_dim = max(max(w, h) for w, h in valid_dims)
    scale = target / global_max_dim

    out: list[Image.Image] = []
    for f in frames:
        rgba = f.convert("RGBA") if f.mode != "RGBA" else f
        w, h = rgba.size
        if w == 0 or h == 0:
            out.append(Image.new("RGBA", (size, size), bg_fill))
            continue
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        resized = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), bg_fill)
        canvas.paste(resized, ((size - nw) // 2, (size - nh) // 2), resized)
        out.append(canvas)
    return out


def _normalize_per_frame_size(
    frames: list[Image.Image],
    transparent_frames: list[Image.Image] | None,
    size: int,
    *,
    bg_fill: tuple[int, int, int, int],
    fit_scale: float,
    align: str = "center",
) -> list[Image.Image]:
    """LANCZOS-resize each frame INDIVIDUALLY so the character's bbox occupies
    a consistent fraction of the canvas across the whole batch.

    Eliminates the visible "breathing" effect where the model rendered some
    panels at slightly different scales than others — `_resize_to_square`
    preserves that drift because it uses ONE shared scale (max bbox), so
    smaller-bbox frames stay smaller on the canvas. Here each frame gets its
    own scale factor based on its own character extent, normalized to a shared
    target along the major axis (height for upright poses, width for horizontal
    poses like asleep/lying-down). Aspect ratio is preserved.

    `transparent_frames`, when paired (same length as `frames`), is used to
    measure the true character bbox via alpha — more accurate than the input
    frame's full size which may include constant padding/background.
    """
    if not frames:
        return frames
    target_extent = max(1, int(round(size * fit_scale)))

    paired_t = (
        transparent_frames
        if transparent_frames is not None and len(transparent_frames) == len(frames)
        else None
    )
    extents: list[tuple[int, int]] = []  # (char_w, char_h) per frame
    for i, f in enumerate(frames):
        src = paired_t[i] if paired_t is not None else f
        rgba = src.convert("RGBA") if src.mode != "RGBA" else src
        bb = rgba.getbbox()
        if bb is not None:
            extents.append((bb[2] - bb[0], bb[3] - bb[1]))
        else:
            extents.append(src.size)

    valid = [e for e in extents if e[0] > 0 and e[1] > 0]
    if not valid:
        return [Image.new("RGBA", (size, size), bg_fill) for _ in frames]
    sw = sorted(w for w, _ in valid)
    sh = sorted(h for _, h in valid)
    med_w = sw[len(sw) // 2]
    med_h = sh[len(sh) // 2]
    use_height = med_h >= med_w  # major axis: height for upright, width for horizontal poses

    out: list[Image.Image] = []
    for f, (cw, ch) in zip(frames, extents):
        rgba = f.convert("RGBA") if f.mode != "RGBA" else f
        fw, fh = rgba.size
        if fw == 0 or fh == 0 or cw == 0 or ch == 0:
            out.append(Image.new("RGBA", (size, size), bg_fill))
            continue
        ref = ch if use_height else cw
        scale = target_extent / ref
        nw = max(1, int(round(fw * scale)))
        nh = max(1, int(round(fh * scale)))
        # Clamp so neither dim exceeds the canvas (off-axis growth from arm
        # gestures etc. shouldn't push the character past the edge).
        if max(nw, nh) > size:
            clamp = size / max(nw, nh)
            nw = max(1, int(round(nw * clamp)))
            nh = max(1, int(round(nh * clamp)))
        resized = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), bg_fill)
        x = (size - nw) // 2
        y = size - nh if align == "bottom" else (size - nh) // 2
        canvas.paste(resized, (x, y), resized)
        out.append(canvas)
    return out


def _saturation_boost_stack(rgb_frames: list[Image.Image], chroma_threshold: int = 60) -> Image.Image | None:
    """Build a tile of replicated high-chroma pixels to boost their weight in
    the quantizer histogram.

    Pillow's MEDIANCUT/FASTOCTREE quantize by frequency; a 1–2 px purple eye
    highlight loses to a flat skin region of thousands of near-grays. We
    duplicate saturated pixels enough times to occupy ~1 frame's worth of
    histogram weight so they earn a palette slot.
    """
    if not rgb_frames:
        return None
    w, h = rgb_frames[0].size
    sat_pixels: list[np.ndarray] = []
    for f in rgb_frames:
        arr = np.asarray(f, dtype=np.uint8)
        r = arr[..., 0].astype(np.int16)
        g = arr[..., 1].astype(np.int16)
        b = arr[..., 2].astype(np.int16)
        chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
        mask = chroma > chroma_threshold
        if mask.any():
            sat_pixels.append(arr[mask])
    if not sat_pixels:
        return None
    pixels = np.concatenate(sat_pixels, axis=0)
    target = w * h
    reps = max(1, target // max(1, len(pixels)))
    pixels = np.tile(pixels, (reps, 1))
    if len(pixels) < target:
        pad = np.repeat(pixels[:1], target - len(pixels), axis=0)
        pixels = np.concatenate([pixels, pad], axis=0)
    pixels = pixels[:target].reshape(h, w, 3).astype(np.uint8)
    return Image.fromarray(pixels, "RGB")


def _save_opaque_gif(frames: list[Image.Image], out_path: Path, duration: int) -> None:
    if not frames:
        raise ValueError("no frames to encode")

    # Build the GIF palette from ALL frames combined (not just frame 1), so
    # rare hues that appear only in some frames — e.g. purple eye highlights
    # when the character looks toward camera, missing in other frames — keep
    # palette slots. Stacking + single quantize is the standard trick used by
    # the transparent-GIF encoder.
    rgb_frames = [f.convert("RGB") for f in frames]
    w, h = rgb_frames[0].size

    boost = _saturation_boost_stack(rgb_frames)
    boost_rows = 1 if boost is not None else 0
    stacked = Image.new("RGB", (w, h * (len(rgb_frames) + boost_rows)))
    for i, f in enumerate(rgb_frames):
        stacked.paste(f, (0, i * h))
    if boost is not None:
        stacked.paste(boost, (0, len(rgb_frames) * h))

    try:
        # libimagequant preserves saturated low-frequency colors much better
        # than MEDIANCUT — needed for tiny pure-color details (eye glints,
        # gem highlights). Falls back if Pillow was built without it.
        paletted_stack = stacked.quantize(
            method=Image.Quantize.LIBIMAGEQUANT,
            colors=256, dither=Image.Dither.FLOYDSTEINBERG,
        )
    except (ValueError, OSError):
        # FASTOCTREE preserves rare colors better than MEDIANCUT; combined
        # with the saturation-boost tile above, this keeps purple eye glints
        # alive even on Pillow builds without libimagequant. Skip dithering —
        # it dilutes 1–2 px highlights across neighbors before quantize.
        paletted_stack = stacked.quantize(
            method=Image.Quantize.FASTOCTREE,
            colors=256, dither=Image.Dither.NONE,
        )

    out_frames = [
        paletted_stack.crop((0, i * h, w, (i + 1) * h)) for i in range(len(rgb_frames))
    ]
    out_frames[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=out_frames[1:],
        duration=duration,
        loop=0,
        disposal=2,
    )


def _compose_strip(frames: list[Image.Image]) -> Image.Image:
    if not frames:
        raise ValueError("no frames to compose")
    w = sum(f.width for f in frames)
    h = max(f.height for f in frames)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x = 0
    for f in frames:
        canvas.paste(f, (x, 0))
        x += f.width
    return canvas


def process_sheet(raw_png: Path, output_dir: Path, opts: ProcessOptions) -> ProcessResult:
    """Slice the codex-generated sheet into N equal vertical strips at native resolution.

    No resampling: we rely on the prompt enforcing an N:1 aspect ratio + equal-width
    panel division. If the model deviates, the metadata records the actual size so
    the user can see drift, but we never resize — that would degrade pixel art and
    mis-align panels worse than equal-share slicing.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = Image.open(raw_png).convert("RGBA")
    raw.save(output_dir / "raw-sheet.png")

    if opts.chroma_key:
        result = _process_chroma(raw, raw_png, output_dir, opts)
    else:
        result = _process_keep_bg(raw, raw_png, output_dir, opts)

    result.raw_size_actual = raw.size
    return result


def _process_chroma(
    raw: Image.Image, raw_png: Path, output_dir: Path, opts: ProcessOptions
) -> ProcessResult:
    cleaned_full = vp.remove_bg_magenta(raw.copy(), opts.threshold, opts.edge_threshold)
    cleaned_full.save(output_dir / "raw-sheet-clean.png")

    frames, frame_qc = vp.split_grid(
        raw,
        opts.rows,
        opts.cols,
        opts.cell_size,
        opts.threshold,
        opts.edge_threshold,
        fit_scale=opts.fit_scale,
        trim_border_px=opts.trim_border,
        edge_clean_depth=opts.edge_clean_depth,
        align=opts.align,
        shared_scale=opts.shared_scale,
        component_mode=opts.component_mode,
        component_padding=opts.component_padding,
        min_component_area=opts.min_component_area,
        edge_touch_margin=opts.edge_touch_margin,
    )

    frame_paths = _save_frames(frames, output_dir, opts.label_prefix)
    sheet_path = output_dir / "sheet-transparent.png"
    vp.compose_sheet(frames, opts.rows, opts.cols, opts.cell_size).save(sheet_path)
    gif_path = output_dir / "animation.gif"
    vp.save_transparent_gif(frames, gif_path, opts.duration_ms)

    edge_touch = [info["grid"] for info in frame_qc if bool(info.get("edge_touch"))]
    meta_path = _write_meta(output_dir, raw_png, opts, frame_qc, edge_touch, mode="chroma")
    return ProcessResult(
        output_dir=output_dir,
        sheet=sheet_path,
        animation_gif=gif_path,
        frame_paths=frame_paths,
        edge_touch_frames=edge_touch,
        meta_path=meta_path,
    )


def _process_keep_bg(
    raw: Image.Image, raw_png: Path, output_dir: Path, opts: ProcessOptions
) -> ProcessResult:
    use_components = opts.component_slice and opts.chroma_key_color is not None
    if use_components:
        # Component-based slicing: chroma key the FULL sheet first, find x-cuts
        # in alpha space, then for each panel pick the largest connected
        # character component nearest the panel center. This recovers limbs
        # that crossed a panel boundary.
        cleaned_full = auto_remove_bg(
            raw,
            opts.rembg_threshold,
            opts.rembg_edge_threshold,
            chroma_key_color=opts.chroma_key_color,
        )
        x_cuts, content_aware = _find_chroma_cuts(
            raw, opts.cols, opts.chroma_key_color, opts.rembg_threshold
        )
        cut_method = "valley"
        if not content_aware:
            # No clean green gap — characters likely too close together. Try
            # locating character peaks in alpha density instead.
            peak_cuts = _find_cuts_via_peaks(cleaned_full, opts.cols)
            if peak_cuts is not None:
                x_cuts = peak_cuts
                cut_method = "peak"
        raw_panel_frames, transparent_panels, qc = _slice_with_components(
            raw, cleaned_full, opts.cols, x_cuts, opts.panel_overlap,
            chroma_key_color=opts.chroma_key_color,
        )
        for entry in qc:
            entry["content_aware_slice"] = content_aware
            entry["cut_method"] = cut_method
        # When we will later LANCZOS-fit to a fixed output_frame_size,
        # _resize_to_square handles centering with a SHARED scale derived from
        # max(bbox) across all frames — pasting each bbox-cropped frame
        # directly preserves character size consistency across frames.
        # Only when output_frame_size is None do we need to manually pad each
        # bbox-cropped frame onto a common canvas (otherwise frames are
        # heterogeneous sizes and the strip/GIF will jitter).
        if opts.output_frame_size:
            transparent_frames = transparent_panels  # bbox-cropped, varying sizes
            frames = raw_panel_frames                # bbox-cropped, varying sizes
        else:
            if opts.recenter_transparent:
                transparent_panels = _recenter_transparent_frames(
                    transparent_panels, align=opts.recenter_align, padding=opts.recenter_padding
                )
            canvas_w, canvas_h = transparent_panels[0].size if transparent_panels else (0, 0)
            chroma_fill = (*opts.chroma_key_color, 255)
            frames = []
            for raw_panel in raw_panel_frames:
                canvas = Image.new("RGBA", (canvas_w, canvas_h), chroma_fill)
                rw, rh = raw_panel.size
                x = (canvas_w - rw) // 2
                y = (canvas_h - rh) // 2
                if opts.recenter_align == "bottom":
                    y = canvas_h - rh - opts.recenter_padding
                canvas.paste(raw_panel, (x, y))
                frames.append(canvas)
            transparent_frames = transparent_panels
    else:
        frames, qc = _slice_keep_bg(
            raw, opts.rows, opts.cols, opts.chroma_key_color, opts.rembg_threshold
        )
        if opts.chroma_key_color is not None:
            frames = _normalize_keep_bg_frames(frames, opts.chroma_key_color)
        transparent_frames = None
        if opts.also_auto_rembg:
            transparent_frames = [
                auto_remove_bg(
                    f, opts.rembg_threshold, opts.rembg_edge_threshold,
                    chroma_key_color=opts.chroma_key_color,
                )
                for f in frames
            ]
            if opts.recenter_transparent:
                transparent_frames = _recenter_transparent_frames(
                    transparent_frames, align=opts.recenter_align, padding=opts.recenter_padding,
                )

    # Optional final resize to a square output canvas (UI's frame_size_px).
    if opts.output_frame_size:
        chroma_rgba = (*opts.chroma_key_color, 255) if opts.chroma_key_color else (0, 0, 0, 255)
        if opts.normalize_per_frame_size:
            # Per-frame scaling locked to a shared character-bbox target —
            # cancels "breathing" size jitter between panels. Pair each with-bg
            # frame with its transparent twin so we measure the TRUE character
            # extent (alpha bbox) instead of the with-bg frame's full size.
            frames = _normalize_per_frame_size(
                frames, transparent_frames,
                opts.output_frame_size, bg_fill=chroma_rgba,
                fit_scale=opts.output_fit_scale,
            )
            if transparent_frames is not None:
                transparent_frames = _normalize_per_frame_size(
                    transparent_frames, transparent_frames,
                    opts.output_frame_size, bg_fill=(0, 0, 0, 0),
                    fit_scale=opts.output_fit_scale,
                )
        else:
            frames = _resize_to_square(
                frames, opts.output_frame_size, bg_fill=chroma_rgba, fit_scale=opts.output_fit_scale,
            )
            if transparent_frames is not None:
                transparent_frames = _resize_to_square(
                    transparent_frames, opts.output_frame_size, bg_fill=(0, 0, 0, 0),
                    fit_scale=opts.output_fit_scale,
                )

    frame_paths = _save_frames(frames, output_dir, opts.label_prefix)
    strip_path = output_dir / "strip.png"
    _compose_strip(frames).save(strip_path)
    gif_path = output_dir / "animation.gif"
    _save_opaque_gif(frames, gif_path, opts.duration_ms)
    qc_summary = _summarize_qc(qc)
    meta_path = _write_meta(
        output_dir, raw_png, opts, qc, [], mode="keep_bg", qc_summary=qc_summary,
    )

    transparent_dir: Path | None = None
    transparent_strip: Path | None = None
    transparent_gif: Path | None = None
    transparent_frame_paths: list[Path] = []
    if transparent_frames is not None and opts.also_auto_rembg:
        transparent_dir = output_dir / "transparent"
        transparent_dir.mkdir(parents=True, exist_ok=True)
        for i, tf in enumerate(transparent_frames):
            p = transparent_dir / f"{opts.label_prefix}-{i + 1:02d}.png"
            tf.save(p)
            transparent_frame_paths.append(p)
        transparent_strip = transparent_dir / "strip.png"
        _compose_strip(transparent_frames).save(transparent_strip)
        transparent_gif = transparent_dir / "animation.gif"
        save_transparent_gif(transparent_frames, transparent_gif, opts.duration_ms)

    return ProcessResult(
        output_dir=output_dir,
        sheet=strip_path,
        animation_gif=gif_path,
        frame_paths=frame_paths,
        meta_path=meta_path,
        transparent_dir=transparent_dir,
        transparent_strip=transparent_strip,
        transparent_gif=transparent_gif,
        transparent_frame_paths=transparent_frame_paths,
        edge_touch_frames=[[0, p] for p in qc_summary.get("edge_touch_panels", [])],
        qc_warnings=qc_summary.get("warnings", []),
        qc_summary=qc_summary,
    )


def _save_frames(frames: list[Image.Image], output_dir: Path, prefix: str) -> list[Path]:
    paths: list[Path] = []
    for i, f in enumerate(frames):
        p = output_dir / f"{prefix}-{i + 1:02d}.png"
        f.save(p)
        paths.append(p)
    return paths


def _write_meta(
    output_dir: Path,
    raw_png: Path,
    opts: ProcessOptions,
    frame_qc: list[dict],
    edge_touch: list[list[int]],
    *,
    mode: str,
    qc_summary: dict | None = None,
) -> Path:
    meta_path = output_dir / "pipeline-meta.json"
    payload = {
        "mode": mode,
        "raw_input": str(raw_png),
        "rows": opts.rows,
        "cols": opts.cols,
        "cell_size": opts.cell_size,
        "frames": frame_qc,
        "edge_touch_frames": edge_touch,
        "auto_rembg": opts.also_auto_rembg,
    }
    if qc_summary is not None:
        payload["qc"] = qc_summary
    meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return meta_path



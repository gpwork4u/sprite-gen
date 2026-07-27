"""Prompt templates.

A template is a function that takes a `TemplateInputs` payload and returns the
final prompt string sent to codex `image_gen`. Templates differ in:
- output format expectations (single sheet vs multi-image sequence)
- background handling (magenta chroma vs reference-style backdrop)
- identity-lock structure (free-form bullets vs strict character schema)

Currently registered:
- "sidescroller_character" : text-or-reference 2D side-scroller animation strip,
                             chroma-green background, per-action frame design
- "static_asset"           : text-or-reference static item/prop/icon sheet,
                             one variation per panel, chroma-green background
- "hd2d_anime_32frame"     : reference-preserving HD-2D anime strip (needs a
                             reference image)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TemplateInputs:
    """Everything a template might need. Templates pick what they use."""

    # required
    animation_type: str           # e.g. "idle breathing loop", "shake cocktail"
    animation_details: str        # free text, what the motion does

    # identity (free-form bullet lines, one per line, no bullets)
    identity_lines: list[str] = field(default_factory=list)

    # multi-image sequence params (used by hd2d_anime_32frame)
    num_images: int = 8
    frames_per_image: int = 4
    image_index: int = 1          # 1-based, which strip in the sequence we're requesting
    frame_size_px: int = 512      # post-process output frame size (recenter canvas)

    # When a single action is split into chunks of unequal size, these let the
    # template number frames against the WHOLE animation instead of assuming a
    # uniform frames_per_image. 0 = derive from image_index/num_images.
    total_frames_override: int = 0
    frame_start_override: int = 0

    # grid params (used by magenta_grid)
    rows: int = 2
    cols: int = 2
    # side | front | back | 3/4 | topdown | topdown_down | topdown_up | topdown_side
    # The three `topdown_*` variants pin the facing direction, which is what a
    # 4-direction top-down RPG character set needs (the _side sheet is mirrored
    # at import time to get the left-facing set).
    view: str = "side"
    # Selects an entry from _STYLE_PRESETS: pixel_art (default, the historical
    # hard-coded HD-2D anime wording) | topdown_rpg | retro_pixel
    art_style: str = "pixel_art"
    # Full override of the style wording. Non-empty values win over art_style.
    style_prefix: str = ""        # replaces the prompt's opening style line
    style_block: str = ""         # replaces the STYLE: block
    notes: str = ""

    # text-to-sprite design (used by sidescroller_character / static_asset).
    # When the caller has NO reference image, these free-form lines fully
    # describe the subject so the model can generate it from scratch. When a
    # reference IS attached (from_reference=True), the design lines act as
    # additional identity locks on top of the image.
    character_design: list[str] = field(default_factory=list)
    from_reference: bool = False

    # static_asset only: each line is one asset variation to place in its own
    # panel (e.g. "red health potion", "blue mana potion"). Empty = single asset.
    variations: list[str] = field(default_factory=list)
    asset_name: str = ""          # short label for the asset family, e.g. "potion"


# Pinned to gpt-image-2 / image_gen supported canvas sizes. Asking for
# arbitrary aspects (e.g. 4:1) does not work — the model picks its own size
# and ignores the prompt's pixel-dimension hints. So we constrain to sizes
# the tool actually emits and back-compute integer panel widths from there.
SUPPORTED_SHEETS: dict[int, tuple[int, int]] = {
    1: (1024, 1024),
    2: (1536, 1024),
    3: (1536, 1024),
    4: (1536, 1024),
}


def sheet_dimensions_for(fpi: int) -> tuple[int, int, int, int]:
    """Return (sheet_w, sheet_h, panel_w, panel_h) for `fpi` panels.

    sheet_w / fpi must be integer to give clean slice cuts. For fpi outside
    the supported map we fall back to 1536×1024 (works up to fpi=8 with
    panels of 192×1024 — narrow but at least slicing is exact).
    """
    sheet_w, sheet_h = SUPPORTED_SHEETS.get(fpi, (1536, 1024))
    panel_w = sheet_w // fpi
    panel_h = sheet_h
    return sheet_w, sheet_h, panel_w, panel_h


def _identity_block(lines: list[str]) -> str:
    if not lines:
        return "- (no identity overrides; preserve everything from the reference image exactly)"
    return "\n".join(f"- {line.strip()}" for line in lines if line.strip())


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line.strip()}" for line in lines if line.strip())


def chroma_background_block() -> str:
    """The STRICT CHROMA KEY background spec, shared by every chroma-green template.

    The post-processor keys out #00B140; this block is what makes that reliable.
    """
    return """BACKGROUND — STRICT CHROMA KEY:
- The ENTIRE background of the image MUST be a single SOLID FLAT pure chroma green: hex #00B140 (RGB 0, 177, 64).
- The chroma green must be perfectly uniform across the whole sheet — NO gradients, NO shading, NO atmospheric perspective, NO lighting variation, NO vignette, NO patterns, NO scenery, NO ground, NO shadow on the background.
- Every pixel that is not part of the subject must be exactly this same chroma green, including the spaces between panels.
- The subject itself MUST NOT contain any pure greens that would be confused with the chroma key — if it uses green, shift those greens away from #00B140 (more saturated, more yellow, or darker) so chroma keying can cleanly separate it.
- This green will be removed in post-processing — it is a chroma key, not part of the scene."""


def panel_layout_block(
    fpi: int, sheet_w: int, sheet_h: int, panel_w: int, panel_h: int,
    *, subject_word: str = "character",
) -> str:
    """The OUTPUT DIMENSIONS + LAYOUT + PLACEMENT + SEPARATION spec.

    These are the constraints the slicer trusts (equal vertical strips, identical
    bounding box, ≤40% fill with thick green margins). `subject_word` lets the
    same rules read naturally for a character, an asset, etc.
    """
    boundaries = ", ".join(str(i * panel_w) for i in range(fpi + 1))
    return f"""OUTPUT IMAGE DIMENSIONS — STRICT, REQUIRED BY image_gen
- Generate the image at EXACTLY {sheet_w} × {sheet_h} pixels. This is a size your image_gen tool natively supports — request this size when calling image_gen.
- The {sheet_w} × {sheet_h} canvas is divided into {fpi} equal vertical panels, each EXACTLY {panel_w} × {panel_h} pixels.
- Panel boundaries fall at integer x positions: {boundaries} pixels along the width.

LAYOUT — ABSOLUTE RULES, NO EXCEPTIONS
- EXACTLY {fpi} equal-width panels arranged left-to-right in ONE horizontal row. ONE row only, no second row.
- Every panel has IDENTICAL width and IDENTICAL height — each panel is {panel_w} × {panel_h} pixels. There must be no width drift between panels.
- The full sheet is exactly {sheet_w} × {sheet_h} pixels, divisible into {fpi} equal {panel_w} × {panel_h} slices.

{subject_word.upper()} PLACEMENT INSIDE EACH PANEL — NON-NEGOTIABLE:
- The {subject_word} (including ALL of its silhouette extensions — hair, weapons, accessories, motion lines, FX, glow, shadow) occupies AT MOST 40% of the panel width and AT MOST 70% of the panel height — it fits inside a {int(panel_w * 0.40)}px × {int(panel_h * 0.70)}px central box, CENTERED both horizontally and vertically inside the {panel_w}×{panel_h} panel.
- Inside each panel, leave chroma-green margin of AT LEAST {int(panel_w * 0.30)}px LEFT, {int(panel_w * 0.30)}px RIGHT, {int(panel_h * 0.15)}px TOP, {int(panel_h * 0.15)}px BOTTOM — a thick band of pure chroma green between the {subject_word} and every panel edge.
- BETWEEN ANY TWO ADJACENT PANELS' SUBJECTS THERE MUST BE AT LEAST {int(panel_w * 0.60)} CONSECUTIVE COLUMNS OF PURE CHROMA GREEN with ZERO subject pixels. Sparse panels are GOOD — pack loosely.
- THE FIRST AND LAST PANELS ARE NOT EXCEPTIONS. The {subject_word} must NEVER come within {int(panel_w * 0.30)}px of the image's left, right, top, or bottom edge — always a thick band of flat chroma green around the entire sheet.
- The {subject_word} has IDENTICAL bounding box, IDENTICAL pixel scale, and IDENTICAL vertical center across ALL {fpi} panels. Same height, same width, same head y-position, same feet y-position in every panel. Only the pose/variation changes between panels.
- NO body part, weapon, hair strand, prop, or shadow may extend into a green margin or cross into a neighboring panel.
- Panels are conceptually independent — there is NO spatial continuity between them. Do not draw one subject stretched across multiple panels.

PANEL SEPARATION:
- ZERO padding around the outer edges of the sheet. ZERO borders, gridlines, dividers, separators, frames, or labels between panels — the panels touch directly, separated only by the flat chroma green.
- DO NOT add a second row. DO NOT pad to a square. DO NOT add letterbox bars. DO NOT add captions.

OUTPUT FORMAT
- ONE horizontal sprite sheet at exactly {sheet_w} × {sheet_h} pixels
- {fpi} panels left-to-right, each {panel_w} × {panel_h} pixels
- no borders, no gridlines, no labels, no text
- game-ready frames
- Call image_gen with size {sheet_w}x{sheet_h}."""


def json_output_footer() -> str:
    return (
        "After image_gen finishes, output ONLY one line of JSON to stdout, nothing else, "
        'no prose, no markdown fences:\n{"raw_png_path": "<absolute path to the generated PNG file>"}'
    )


def hd2d_anime_32frame(inp: TemplateInputs) -> str:
    """User-supplied HD-2D anime template.

    Reference image is the DIRECT BASE FRAME. Identity locks are bullet lines.
    Each call produces ONE horizontal strip of `frames_per_image` frames; the
    template states this is image `image_index` of `num_images` total so the
    model knows the timing slice.
    """
    fpi = inp.frames_per_image
    n = inp.num_images
    idx = inp.image_index
    sheet_w, sheet_h, panel_w, panel_h = sheet_dimensions_for(fpi)

    frame_start = (idx - 1) * fpi + 1
    frame_end = idx * fpi
    total_frames = n * fpi

    return f"""anime pixel art, modern HD-2D anime sprite style, clean pixel line art, soft pixel shading, minimal rendering drift, consistent anime character identity, frame-consistent sprite animation

REFERENCE GUIDED ANIMATION — VERY IMPORTANT

Use the attached reference image as the DIRECT BASE FRAME.
The animation must look like the ORIGINAL IMAGE itself was animated.

DO NOT redesign the character.
DO NOT reinterpret the art style.
DO NOT alter outfit details.
DO NOT alter face shape.
DO NOT alter proportions.
DO NOT change composition.

Maintain EXACT:
{_identity_block(inp.identity_lines)}

STYLE:
high quality anime pixel art,
clean outlines,
soft pixel shading,
simple lighting,
minimal shading,
2D anime illustration style,
no realistic rendering,
stable sprite consistency

BACKGROUND — STRICT CHROMA KEY:
- The ENTIRE background of the image MUST be a single SOLID FLAT pure chroma green: hex #00B140 (RGB 0, 177, 64).
- The chroma green must be perfectly uniform across the whole sheet — NO gradients, NO shading, NO atmospheric perspective, NO lighting variation, NO vignette, NO patterns, NO scenery, NO ground, NO shadow on the background.
- Every pixel that is not part of the character body must be exactly this same chroma green, including the spaces between panels.
- The character itself MUST NOT contain any pure greens that would be confused with the chroma key — if the reference outfit/hair/skin uses green, shift those greens away from #00B140 (more saturated, more yellow, or darker) so chroma keying can cleanly separate the character.
- This green will be removed in post-processing — it is a chroma key, not part of the scene.
- Do NOT preserve the background of the reference image. Replace it entirely with the flat chroma green described above.

COMPOSITION:
- preserve the camera angle, the character's pose silhouette family, and the apparent camera distance from the reference image
- static camera
- centered composition
- IMPORTANT: the reference image's tight framing does NOT apply to each panel. Each panel adds equal chroma-green margin around the character so the character occupies ~40% of the panel width (see LAYOUT). Do not crop the character to the panel edges; leave a thick band of green breathing room on all sides.

ANIMATION TYPE:
{inp.animation_type}

ANIMATION DETAILS:
{inp.animation_details}

IMPORTANT GENERATION RULE:
Generate ONLY {fpi} frames per image.
This image contains: Frame {frame_start}, Frame {frame_start + 1}, Frame {frame_start + 2}, Frame {frame_end}.

This is image {idx} of {n} in a sequence continuation. The FULL animation contains {total_frames} frames split across {n} sprite sheets.
- Image 1 = Frames 1~{fpi}
- Image 2 = Frames {fpi + 1}~{fpi * 2}
- ...
- Image {n} = Frames {total_frames - fpi + 1}~{total_frames}

You are now producing IMAGE {idx} (frames {frame_start}~{frame_end}). Match the timing slice precisely; assume image {idx - 1} ended at frame {frame_start - 1 if idx > 1 else 0} and image {idx + 1} will start at frame {frame_end + 1 if idx < n else total_frames}.

Maintain perfectly consistent:
- character proportions
- camera angle
- lighting
- outfit details
- facial features
- background framing

MOTION RULES:
- preserve original pose foundation
- smooth animation progression
- readable keyframe motion
- no exaggerated deformation
- no perspective changes
- no scene changes
- no style drift

FRAME DESIGN:
Frame {frame_start} = neutral/start motion of this slice
Frame {frame_start + 1} = movement transition
Frame {frame_start + 2} = strongest motion of this slice
Frame {frame_end} = recovery / handoff to next image

OUTPUT IMAGE DIMENSIONS — STRICT, REQUIRED BY image_gen
- Generate the image at EXACTLY {sheet_w} × {sheet_h} pixels. This is a size your image_gen tool natively supports — request this size when calling image_gen.
- The {sheet_w} × {sheet_h} canvas is divided into {fpi} equal vertical panels, each EXACTLY {panel_w} × {panel_h} pixels.
- Panel boundaries fall at integer x positions: { ", ".join(str(i * panel_w) for i in range(fpi + 1)) } pixels along the width.

LAYOUT — ABSOLUTE RULES, NO EXCEPTIONS
- EXACTLY {fpi} equal-width panels arranged left-to-right in ONE horizontal row. ONE row only, no second row.
- Every panel has IDENTICAL width and IDENTICAL height — each panel is {panel_w} × {panel_h} pixels. There must be no width drift between panels — panel 1, panel 2, ..., panel {fpi} all measure exactly {panel_w} pixels wide.
- The full sheet is exactly {sheet_w} × {sheet_h} pixels, divisible into {fpi} equal {panel_w} × {panel_h} slices.

CHARACTER PLACEMENT INSIDE EACH PANEL — NON-NEGOTIABLE:
- The character (including ALL of its hair, weapons, accessories, motion lines, FX, glow, shadow, and any visual extension) occupies AT MOST 40% of the panel width and AT MOST 70% of the panel height — that is, the character fits inside a {int(panel_w * 0.40)}px × {int(panel_h * 0.70)}px central box, CENTERED both horizontally and vertically inside the {panel_w}×{panel_h} panel.
- Inside each panel, this leaves chroma-green margin of AT LEAST {int(panel_w * 0.30)}px on the LEFT, AT LEAST {int(panel_w * 0.30)}px on the RIGHT, AT LEAST {int(panel_h * 0.15)}px on the TOP, and AT LEAST {int(panel_h * 0.15)}px on the BOTTOM — every side of the character must have a thick band of pure chroma green between the character and the panel edge.
- BETWEEN ANY TWO ADJACENT CHARACTERS THERE MUST BE AT LEAST {int(panel_w * 0.60)} CONSECUTIVE COLUMNS OF PURE CHROMA GREEN with ZERO character pixels (no hair strand, no sword tip, no motion streak, no glow, no shadow). If two adjacent characters are visually closer than {int(panel_w * 0.60)}px of solid green, the post-processor will mis-crop them. Sparse panels are GOOD — pack characters loosely.
- THE FIRST AND LAST PANELS ARE NOT EXCEPTIONS. The leftmost panel must have its full ≥{int(panel_w * 0.30)}px green margin on its LEFT side (between the character and the image's left edge at x=0). The rightmost panel must have its full ≥{int(panel_w * 0.30)}px green margin on its RIGHT side (between the character and the image's right edge at x={sheet_w}). The character must NEVER come within {int(panel_w * 0.30)}px of the image's left, right, top, or bottom edge — there must always be a thick band of flat chroma green around the entire sheet.
- The character has IDENTICAL bounding box, IDENTICAL pixel scale, and IDENTICAL vertical center across ALL {fpi} panels. Same height in every panel, same width in every panel, same y-position of the head, same y-position of the feet. Only the pose changes between panels.
- NO body part, weapon, hair strand, sword tip, cape edge, prop, or shadow may extend into the left or right green margin or cross into the neighboring panel. The motion must stay inside the central 40% of each panel — even at the strongest motion frame, every visible character pixel stays inside the central {int(panel_w * 0.40)}px-wide band.
- Panels are conceptually independent — there is NO spatial continuity between them. Do not draw the character once stretched across multiple panels.

PANEL SEPARATION:
- ZERO padding around the outer edges of the sheet. ZERO borders, gridlines, dividers, separators, frames, or labels between panels — the panels touch directly. The only thing visible between two panels' characters is the flat chroma green described in BACKGROUND.
- DO NOT add a second row. DO NOT pad to a square. DO NOT add letterbox bars. DO NOT add captions.

The required output size is {sheet_w} × {sheet_h} pixels — request this size from image_gen explicitly. The equal-width panel division, identical character bounding box, and ≤40% character fill with thick green margins are ALL non-negotiable. The post-processor slices into {fpi} equal vertical strips at panel_w={panel_w} and trusts these constraints; any drift in panel widths or character placement will mis-crop the frames.

OUTPUT FORMAT
- ONE horizontal sprite sheet at exactly {sheet_w} × {sheet_h} pixels
- {fpi} panels left-to-right, each {panel_w} × {panel_h} pixels
- no borders, no gridlines, no labels, no text
- game-ready animation frames
- Call image_gen with size {sheet_w}x{sheet_h}.

After image_gen finishes, output ONLY one line of JSON to stdout, nothing else, no prose, no markdown fences:
{{"raw_png_path": "<absolute path to the generated PNG file>"}}"""


# ─────────────────────── side-scroller character template ───────────────────

# Canonical motion phases per action, authored for a 2D side-scroller (platformer)
# where the character faces RIGHT, full body, feet on a common groundline. Each
# list is the "ideal" full breakdown; _resample_phases maps it onto whatever
# frames_per_image the caller asked for so 3/4/5/6/8-frame strips all read well.
SIDESCROLLER_ACTIONS: dict[str, list[str]] = {
    "idle": [
        "neutral standing rest, weight centered",
        "subtle inhale, chest/shoulders rise slightly",
        "settle, weight shifts a touch",
        "exhale back toward neutral (loops to frame 1)",
    ],
    "walk": [
        "contact: lead foot forward, heel down",
        "down/recoil: weight over front leg, body lowest",
        "passing: rear leg swings under hips, body rising",
        "high point: push-off, body highest",
        "contact mirrored: other foot forward",
        "passing back toward frame 1 (seamless loop)",
    ],
    "run": [
        "contact, lead foot strikes, body leaning forward",
        "down/compression, deep knee bend",
        "drive, explosive push-off",
        "full suspension airborne, both feet off ground",
        "opposite contact",
        "recovery toward frame 1 (loop)",
    ],
    "jump": [
        "crouch / anticipation, knees bent low",
        "explosive take-off, legs extending",
        "rising, arms up, body stretched",
        "apex, peak airtime, legs tucked",
        "descent, legs reaching for ground",
        "landing crouch, impact absorbed",
    ],
    "attack": [
        "wind-up / anticipation, weapon drawn back",
        "step-in, weight shifting forward",
        "peak strike, weapon fully extended, strongest pose",
        "follow-through past the target",
        "recoil",
        "recovery to neutral stance",
    ],
    "hurt": [
        "impact moment, body absorbing the hit",
        "deepest knockback, head snapped back",
        "stagger recovery toward neutral",
    ],
    "death": [
        "hit reaction",
        "stagger, losing balance",
        "fall begins, body tipping",
        "collapse",
        "ground impact",
        "final still pose on the ground",
    ],
    "cast": [
        "gather, drawing energy in",
        "channel, energy building at hands",
        "peak charge, strongest glow",
        "release, energy thrust outward",
        "recoil",
        "recovery to neutral",
    ],
    "crouch": [
        "begin lowering from standing",
        "mid crouch",
        "full crouch, stable held pose",
        "rising back toward standing",
    ],
}


def _resample_phases(phases: list[str], fpi: int) -> list[str]:
    """Map an ideal phase list onto exactly `fpi` frames via linear index sampling."""
    if fpi <= 0:
        return []
    if len(phases) == fpi:
        return phases
    out: list[str] = []
    for i in range(fpi):
        src = round(i * (len(phases) - 1) / max(1, fpi - 1)) if fpi > 1 else 0
        out.append(phases[min(src, len(phases) - 1)])
    return out


def _action_frame_design(action: str, total_frames: int, frame_start: int, count: int) -> str:
    """Frame design for `count` panels starting at absolute `frame_start`.

    Phases are resampled across the WHOLE `total_frames` animation first, then we
    slice out this chunk's portion — so chunk 2 of a walk shows the second-half
    poses (frames 4..6) rather than restarting at frame 1.
    """
    phases = SIDESCROLLER_ACTIONS.get(
        action.lower().strip(),
        [f"{action} pose, phase {i + 1}" for i in range(total_frames)],
    )
    full = _resample_phases(phases, total_frames)
    lines = []
    for i in range(count):
        abs_idx = frame_start + i           # 1-based absolute frame number
        desc = full[min(abs_idx - 1, len(full) - 1)]
        lines.append(f"Frame {abs_idx} = {desc}")
    return "\n".join(lines)


_TOPDOWN_COMMON = (
    "TOP-DOWN 3/4 view for a top-down RPG (Zelda / Stardew style camera). "
    "The camera looks DOWN at the character at a 60° pitch — you see the top of the head and the "
    "shoulders clearly, and the ground plane recedes upward. This is NOT a flat 90° bird's-eye view "
    "(the face must stay readable) and NOT a side-scroller profile. "
    "Full body visible. Keep the SAME 60° camera pitch in every panel — do NOT let perspective drift "
    "between panels, and do NOT tilt the camera toward a side view in any frame."
)

_VIEW_DESCRIPTIONS: dict[str, str] = {
    "side": (
        "STRICT 90° SIDE / PROFILE view — a 2D side-scroller / platformer sprite. "
        "The character faces fully to the RIGHT, in PURE profile: the torso, hips, and "
        "shoulders are seen EDGE-ON (turned 90° away from the camera), NOT facing the viewer. "
        "Only ONE side of the face is visible (one eye, one ear); the nose points right. "
        "This is NOT a 3/4 view and NOT a front-facing pose — do not rotate the torso toward "
        "the camera. Full body visible, feet on a common groundline"
    ),
    "front": "FRONT view, character facing the camera, full body visible",
    "back": "BACK view, character seen from behind, full body visible",
    "3/4": "3/4 view from slightly above, full body visible",
    # Generic top-down. Prefer the direction-locked variants below for 4-direction
    # character sets — they pin the facing so the three sheets stay consistent.
    "topdown": _TOPDOWN_COMMON,
    "topdown_down": (
        _TOPDOWN_COMMON + " The character faces TOWARD the camera (walking DOWN / south, "
        "toward the viewer): both eyes visible, chest and toes pointing at the viewer."
    ),
    "topdown_up": (
        _TOPDOWN_COMMON + " The character faces AWAY from the camera (walking UP / north, "
        "away from the viewer): the back of the head and the back of the torso are what we see. "
        "The face is NOT visible."
    ),
    "topdown_side": (
        _TOPDOWN_COMMON + " The character faces to the RIGHT (walking EAST) in a top-down 3/4 "
        "three-quarter profile: ONE side of the face is visible plus a hint of the far cheek, "
        "shoulders angled, and the top of the head still clearly in view. "
        "Keep the SAME right-facing orientation in every panel — this sheet is mirrored "
        "horizontally at import time to produce the left-facing set, so it must never drift "
        "toward front-facing or toward a flat side profile."
    ),
}

# Views whose subject stands on a ground plane, so "feet on a constant groundline"
# is a meaningful constraint. Top-down has no groundline — anchoring the FEET there
# makes the model shrink/shift the body, so those views anchor the BODY CENTRE instead.
_GROUNDLINE_VIEWS = frozenset({"side", "front", "back", "3/4"})


# ── Art-style presets ────────────────────────────────────────────────────────
# The opening line and STYLE block used to be hard-coded, which made the whole
# generator produce HD-2D anime side-scroller art regardless of the target game.
# `pixel_art` reproduces that original wording byte-for-byte so existing specs
# are unaffected; pass `art_style: <key>` (or `style_prefix` / `style_block` for
# full control) in the pack YAML to select another look.
_STYLE_PRESETS: dict[str, tuple[str, str]] = {
    "pixel_art": (
        "anime pixel art, modern HD-2D 2D side-scroller sprite style, clean pixel line art, "
        "soft pixel shading, consistent character identity, frame-consistent sprite animation",
        "high quality anime pixel art, clean outlines, soft pixel shading, simple lighting, "
        "2D illustration style, no realistic rendering, stable sprite consistency",
    ),
    "topdown_rpg": (
        "warm hand-crafted pixel art for a top-down 2D RPG, cosy storybook palette, "
        "clean readable silhouette, consistent character identity, frame-consistent sprite animation",
        "warm hand-painted pixel art, selective 1px outlines in a darkened hue-shifted tone "
        "(never pure black), soft two-to-three step shading, single light source from the upper "
        "left, muted saturation with restrained highlights, no realistic rendering, no gradients, "
        "no anti-aliased soft edges, stable sprite consistency",
    ),
    "retro_pixel": (
        "retro 16-bit pixel art, limited palette, chunky readable pixels, "
        "consistent character identity, frame-consistent sprite animation",
        "retro 16-bit console pixel art, hard 1px outlines, flat two-tone shading, "
        "no gradients, no anti-aliasing, stable sprite consistency",
    ),
}


def _style_for(inp: "TemplateInputs") -> tuple[str, str]:
    """Resolve (opening line, STYLE block) for this request.

    Explicit `style_prefix` / `style_block` win; otherwise the `art_style` preset
    is used, falling back to `pixel_art` (the historical hard-coded wording).
    """
    preset = _STYLE_PRESETS.get(inp.art_style, _STYLE_PRESETS["pixel_art"])
    return (
        (getattr(inp, "style_prefix", "") or preset[0]),
        (getattr(inp, "style_block", "") or preset[1]),
    )


def sidescroller_character(inp: TemplateInputs) -> str:
    """Text-or-reference driven 2D side-scroller character animation strip.

    Produces ONE horizontal strip of `frames_per_image` panels for a single
    action (animation_type). Works WITHOUT a reference image: when
    `character_design` lines are supplied and `from_reference` is False, the
    model designs the character from text. When a reference is attached
    (from_reference=True), the design lines act as extra identity locks.
    """
    fpi = inp.frames_per_image
    idx = inp.image_index
    n = inp.num_images
    sheet_w, sheet_h, panel_w, panel_h = sheet_dimensions_for(fpi)
    total_frames = inp.total_frames_override or (n * fpi)
    frame_start = inp.frame_start_override or ((idx - 1) * fpi + 1)

    view = _VIEW_DESCRIPTIONS.get(inp.view, _VIEW_DESCRIPTIONS["side"])
    view_extra = (
        "\n- EVERY panel keeps the SAME strict right-facing profile; do NOT let any panel "
        "drift into a 3/4 or front-facing pose"
        if inp.view == "side" else ""
    )
    # Top-down has no groundline; anchoring the feet there makes the model shrink
    # or shift the body between panels. Anchor the body centre instead.
    anchor_line = (
        "the character's feet rest on the SAME horizontal groundline in every panel "
        "(do not draw the ground itself — only keep the feet at a constant y)"
        if inp.view in _GROUNDLINE_VIEWS else
        "the character's BODY CENTRE sits at the SAME x and y position in every panel, at the "
        "SAME scale (do not draw any ground, floor tiles, or cast shadow — the character floats "
        "alone on the flat chroma background). Do NOT anchor the feet to a horizontal line; there "
        "is no ground plane in this view"
    )
    style_prefix, style_block = _style_for(inp)
    design = inp.character_design or inp.identity_lines

    if inp.from_reference:
        source_block = (
            "REFERENCE GUIDED — VERY IMPORTANT\n"
            "Use the attached reference image as the canonical character design. Do NOT redesign, "
            "reinterpret the art style, alter the outfit, face, or proportions.\n\n"
            "Maintain EXACTLY these identity markers:\n" + _identity_block(design)
        )
    else:
        source_block = (
            "CHARACTER DESIGN — GENERATE FROM THIS DESCRIPTION (no reference image is attached)\n"
            "Design a single, consistent character matching ALL of the following, then animate it. "
            "The SAME character — same proportions, palette, outfit, and features — must appear in every panel.\n\n"
            + (_bullets(design) if design else "- (no design given; invent a clean, readable game character)")
        )

    return f"""{style_prefix}

{source_block}

VIEW / CAMERA:
- {view}
- static camera, identical camera distance and angle in every panel
- {anchor_line}{view_extra}

STYLE:
{style_block}

{chroma_background_block()}

ANIMATION TYPE: {inp.animation_type}

ANIMATION DETAILS:
{inp.animation_details or "(smooth, readable, game-ready motion for this action)"}

FRAME-BY-FRAME MOTION DESIGN (this strip = frames {frame_start}..{frame_start + fpi - 1} of {total_frames} total):
{_action_frame_design(inp.animation_type, total_frames, frame_start, fpi)}

MOTION RULES:
- the SAME character in every panel; only the pose changes
- smooth, readable keyframe progression; this action should loop cleanly when applicable
- no exaggerated deformation, no perspective changes, no scene changes, no style drift
- identical proportions, palette, outfit, lighting, and facial features across all panels

{panel_layout_block(fpi, sheet_w, sheet_h, panel_w, panel_h, subject_word="character")}

{json_output_footer()}"""


def static_asset(inp: TemplateInputs) -> str:
    """Text-or-reference driven STATIC game asset (item / prop / weapon / icon).

    No animation. Produces either a single asset (variations empty → fpi=1) or
    one panel per variation on a chroma-green sheet for clean per-asset keying.
    """
    variations = [v.strip() for v in inp.variations if v.strip()]
    fpi = max(1, len(variations)) if variations else 1
    sheet_w, sheet_h, panel_w, panel_h = sheet_dimensions_for(fpi)
    design = inp.character_design or inp.identity_lines
    name = inp.asset_name or inp.animation_type or "game asset"

    if inp.from_reference:
        source_block = (
            "REFERENCE GUIDED: use the attached image as the canonical design / style anchor. "
            "Match its art style, palette, and rendering exactly.\n\n"
            "Design notes:\n" + _identity_block(design)
        )
    else:
        source_block = (
            "DESIGN FROM THIS DESCRIPTION (no reference image is attached):\n"
            + (_bullets(design) if design else f"- a clean, readable {name}")
        )

    if variations:
        panel_lines = "\n".join(
            f"- Panel {i + 1}: {v}" for i, v in enumerate(variations)
        )
        what_block = (
            f"Render EXACTLY {fpi} distinct variations of the {name}, one per panel, "
            "all in the SAME art style, palette family, scale, and lighting:\n" + panel_lines
        )
    else:
        what_block = (
            f"Render a single {name}, centered, as one clean game-ready sprite."
        )

    return f"""game asset pixel art, clean pixel line art, soft pixel shading, crisp readable silhouette, consistent style

{source_block}

WHAT TO DRAW:
{what_block}

STYLE:
clean pixel-art game asset, crisp dark outline, readable silhouette, consistent palette, simple soft shading, no text, no labels, no UI, no drop-shadow on the background

{chroma_background_block()}

{panel_layout_block(fpi, sheet_w, sheet_h, panel_w, panel_h, subject_word="asset")}

{json_output_footer()}"""


REGISTRY: dict[str, Callable[[TemplateInputs], str]] = {
    "hd2d_anime_32frame": hd2d_anime_32frame,
    "sidescroller_character": sidescroller_character,
    "static_asset": static_asset,
}

# Templates that can run with NO reference image (pure text-to-sprite).
TEXT_TO_SPRITE_TEMPLATES: frozenset[str] = frozenset(
    {"sidescroller_character", "static_asset"}
)


def render(template_id: str, inputs: TemplateInputs) -> str:
    if template_id not in REGISTRY:
        raise ValueError(f"unknown template '{template_id}'. available: {sorted(REGISTRY)}")
    return REGISTRY[template_id](inputs)

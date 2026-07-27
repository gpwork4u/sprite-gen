"""Asset-pack driver — text-to-sprite, no reference image required.

Given a single YAML spec describing one or more characters (each with a list of
side-scroller actions) and/or static assets, this fans out the whole pack:

    <output_root>/<id>/<action>/   per-action animation (frames/ + strip + gif + transparent)
    <output_root>/<id>/            static asset (one panel per variation)

Everything is driven by free-form text `design:` lines — no baseline image is
attached to codex, so the character is invented by the model and held consistent
across panels by the layout constraints + QC-retry loop.

Run:
    python -m sprite_gen pack examples/pack.yaml --dry-run   # write prompts only
    python -m sprite_gen pack examples/pack.yaml             # real generation
"""

from __future__ import annotations

import logging
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .codex_runner import CodexInvocationError, run_codex_image_gen
from .postprocess import ProcessOptions, process_sheet
from .templates import (
    TEXT_TO_SPRITE_TEMPLATES,
    TemplateInputs,
    render,
)

log = logging.getLogger("pixelforge.pack")

# Codex destabilises past ~4 panels in one call; longer strips are chunked.
MAX_PANELS_PER_CALL = 4


# ───────────────────────────────── spec model ───────────────────────────────

@dataclass
class ActionSpec:
    action: str
    frames: int = 4
    notes: str = ""
    view: str = "side"


@dataclass
class CharacterSpec:
    id: str
    design: list[str]
    actions: list[ActionSpec]
    view: str = "side"
    template: str = "sidescroller_character"
    frame_size_px: int = 384
    duration_ms: int = 120
    qc_max_retries: int = 4
    art_style: str = "pixel_art"   # see templates._STYLE_PRESETS
    style_prefix: str = ""         # full override of the opening style line
    style_block: str = ""          # full override of the STYLE: block


@dataclass
class AssetSpec:
    id: str
    design: list[str]
    variations: list[str] = field(default_factory=list)
    name: str = ""
    template: str = "static_asset"
    frame_size_px: int = 384
    qc_max_retries: int = 3
    art_style: str = "pixel_art"
    style_prefix: str = ""
    style_block: str = ""


@dataclass
class PackConfig:
    output_root: Path
    characters: list[CharacterSpec] = field(default_factory=list)
    assets: list[AssetSpec] = field(default_factory=list)
    max_workers: int = 2


def _as_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [ln.strip() for ln in value.splitlines() if ln.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def load_pack(path: Path, output_root: Path | None = None) -> PackConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults", {}) or {}

    root = output_root or Path(raw.get("output_root", ".sprites"))

    def d(key: str, fallback: Any) -> Any:
        return defaults.get(key, fallback)

    characters: list[CharacterSpec] = []
    for c in raw.get("characters", []) or []:
        actions = [
            ActionSpec(
                action=a["action"],
                frames=int(a.get("frames", d("frames_per_image", 4))),
                notes=a.get("notes", ""),
                view=a.get("view", c.get("view", d("view", "side"))),
            )
            for a in c.get("actions", []) or []
        ]
        characters.append(
            CharacterSpec(
                id=c["id"],
                design=_as_lines(c.get("design")),
                actions=actions,
                view=c.get("view", d("view", "side")),
                template=c.get("template", d("template", "sidescroller_character")),
                frame_size_px=int(c.get("frame_size_px", d("frame_size_px", 384))),
                duration_ms=int(c.get("duration_ms", d("duration_ms", 120))),
                qc_max_retries=int(c.get("qc_max_retries", d("qc_max_retries", 4))),
                art_style=c.get("art_style", d("art_style", "pixel_art")),
                style_prefix=c.get("style_prefix", d("style_prefix", "")),
                style_block=c.get("style_block", d("style_block", "")),
            )
        )

    assets: list[AssetSpec] = []
    for a in raw.get("assets", []) or []:
        assets.append(
            AssetSpec(
                id=a["id"],
                design=_as_lines(a.get("design")),
                variations=_as_lines(a.get("variations")),
                name=a.get("name", ""),
                template=a.get("template", "static_asset"),
                frame_size_px=int(a.get("frame_size_px", d("frame_size_px", 384))),
                qc_max_retries=int(a.get("qc_max_retries", d("qc_max_retries", 3))),
                art_style=a.get("art_style", d("art_style", "pixel_art")),
                style_prefix=a.get("style_prefix", d("style_prefix", "")),
                style_block=a.get("style_block", d("style_block", "")),
            )
        )

    return PackConfig(
        output_root=root,
        characters=characters,
        assets=assets,
        max_workers=int(raw.get("max_workers", defaults.get("max_workers", 2))),
    )


# ─────────────────────────────── chunk planning ─────────────────────────────

def chunk_sizes(fpi: int, max_per_chunk: int = MAX_PANELS_PER_CALL) -> list[int]:
    """Split `fpi` panels into balanced chunks of <= max_per_chunk (4→[4], 6→[3,3], 8→[4,4])."""
    if fpi <= max_per_chunk:
        return [fpi]
    n = (fpi + max_per_chunk - 1) // max_per_chunk
    base, rem = divmod(fpi, n)
    return [base + 1 if i < rem else base for i in range(n)]


# ─────────────────────────────── outcome model ──────────────────────────────

@dataclass
class UnitOutcome:
    label: str          # e.g. "knight/walk" or "potions"
    output_dir: Path
    ok: bool
    elapsed_s: float = 0.0
    frame_count: int = 0
    strip: Path | None = None
    gif: Path | None = None
    transparent_gif: Path | None = None
    qc_warnings: list[str] = field(default_factory=list)
    error: str | None = None
    dry_run: bool = False


# ──────────────────────────────── generation ────────────────────────────────

def _retry_preamble(attempt_n: int, max_n: int, warnings: list[str]) -> str:
    issues = "\n".join(f"- {w}" for w in warnings)
    return (
        f"RETRY ATTEMPT {attempt_n} of {max_n} — PREVIOUS ATTEMPT FAILED AUTOMATED QC\n\n"
        "The previous generation had these problems detected by post-processing:\n"
        f"{issues}\n\n"
        "Fix ALL of them: leave a THICKER chroma-green margin around every subject, use the SAME "
        "bounding-box size across all panels, keep each subject inside the central 40% of its panel, "
        "and reduce the subject size 10–15% to guarantee margins. Re-read the LAYOUT / BACKGROUND "
        "sections below and follow the pixel constraints literally.\n"
    )


def _chunk_preamble(chunk_idx: int, count: int, chunk_fpi: int, offset: int, total: int) -> str:
    return (
        f"CHUNK CONTINUATION — chunk {chunk_idx + 1} of {count}\n\n"
        "This animation is generated in smaller passes for stability. The PREVIOUS chunk's strip is "
        f"attached as a reference; it rendered frames 1..{offset - 1} of a {total}-frame animation.\n"
        f"Render frames {offset}..{offset + chunk_fpi - 1} — the next {chunk_fpi} frames of the SAME "
        f"animation, on a sheet with EXACTLY {chunk_fpi} panels. Treat the previous chunk's RIGHTMOST "
        "panel as the visual state your LEFTMOST panel continues from: same character identity, scale, "
        "camera distance, lighting. Match colours pixel-faithfully — the strips are concatenated.\n"
    )


def _process_opts(
    fpi: int, frame_size_px: int, duration_ms: int, label_prefix: str, *, static: bool
) -> ProcessOptions:
    # Same flow hd2d uses: keep_bg slicing + auto chroma-green removal to a
    # parallel transparent bundle, fixed square output frames.
    #
    # normalize_per_frame_size: per-frame bbox normalization makes every frame's
    # character fill the same box — desirable for STATIC variation sheets (each
    # asset looks uniformly sized) but it turns frame-to-frame bbox jitter (a
    # raised sword, a hair tip, motion FX) into visible body-size "breathing" on
    # ANIMATIONS. Animations therefore use a single shared scale across all
    # frames so the body stays a constant size and only the pose changes.
    return ProcessOptions(
        rows=1,
        cols=fpi,
        chroma_key=False,
        also_auto_rembg=True,
        duration_ms=duration_ms,
        label_prefix=label_prefix,
        output_frame_size=frame_size_px,
        normalize_per_frame_size=static,
    )


def _build_inputs(
    *, template: str, animation_type: str, animation_details: str, design: list[str],
    view: str, num_images: int, frames_per_image: int, image_index: int,
    frame_size_px: int, variations: list[str], asset_name: str,
    art_style: str = "pixel_art", style_prefix: str = "", style_block: str = "",
    from_reference: bool = False,
) -> TemplateInputs:
    return TemplateInputs(
        animation_type=animation_type,
        animation_details=animation_details,
        character_design=design,
        from_reference=from_reference,
        art_style=art_style,
        style_prefix=style_prefix,
        style_block=style_block,
        view=view,
        num_images=num_images,
        frames_per_image=frames_per_image,
        image_index=image_index,
        frame_size_px=frame_size_px,
        variations=variations,
        asset_name=asset_name,
    )


def _generate_unit(
    *,
    label: str,
    output_dir: Path,
    template: str,
    animation_type: str,
    animation_details: str,
    design: list[str],
    view: str,
    total_frames: int,
    frame_size_px: int,
    duration_ms: int,
    qc_max_retries: int,
    variations: list[str],
    asset_name: str,
    dry_run: bool,
    art_style: str = "pixel_art",
    style_prefix: str = "",
    style_block: str = "",
) -> UnitOutcome:
    """Generate ONE strip (a character action, or a static-asset variation sheet)."""
    started = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Static assets are never chunked (one panel per variation, capped by caller);
    # character animations chunk past 4 panels.
    chunks = [total_frames] if template == "static_asset" else chunk_sizes(total_frames)
    n_chunks = len(chunks)

    def prompt_for(chunk_idx: int, chunk_fpi: int, offset: int) -> str:
        inp = _build_inputs(
            template=template,
            animation_type=animation_type,
            animation_details=animation_details,
            design=design,
            view=view,
            num_images=1,
            frames_per_image=chunk_fpi,
            image_index=1,
            frame_size_px=frame_size_px,
            variations=variations,
            asset_name=asset_name,
            art_style=art_style,
            style_prefix=style_prefix,
            style_block=style_block,
            # Chunks after the first DO get the previous strip attached as a
            # reference image (see extra_reference_images below), so the body must
            # not claim "no reference image is attached" — that contradiction was
            # degrading chunk-to-chunk consistency.
            from_reference=(chunk_idx > 0),
        )
        # Number frames against the whole animation so chunk N shows its real slice.
        inp.total_frames_override = total_frames
        inp.frame_start_override = offset
        body = render(template, inp)
        if chunk_idx > 0:
            return _chunk_preamble(chunk_idx, n_chunks, chunk_fpi, offset, total_frames) + "\n\n" + body
        return body

    # ── dry-run: write prompts only ──
    if dry_run:
        offset = 1
        for ci, cfpi in enumerate(chunks):
            sub = output_dir if n_chunks == 1 else output_dir / f"chunk-{ci + 1:02d}"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "prompt.txt").write_text(prompt_for(ci, cfpi, offset), encoding="utf-8")
            offset += cfpi
        return UnitOutcome(
            label=label, output_dir=output_dir, ok=True,
            elapsed_s=time.monotonic() - started, frame_count=total_frames, dry_run=True,
        )

    # ── real generation: per-chunk QC-retry, then concatenate frames ──
    from PIL import Image

    all_frames: list[Path] = []
    all_tframes: list[Path] = []
    qc_all: list[str] = []
    prev_strip: Path | None = None
    offset = 1

    for ci, cfpi in enumerate(chunks):
        chunk_dir = output_dir if n_chunks == 1 else output_dir / f"chunk-{ci + 1:02d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        base_prompt = prompt_for(ci, cfpi, offset)

        best = None
        prior: list[str] = []
        for attempt in range(1, max(1, qc_max_retries) + 1):
            adir = chunk_dir / f"attempt-{attempt:02d}"
            adir.mkdir(parents=True, exist_ok=True)
            prompt = base_prompt
            if prior:
                prompt = _retry_preamble(attempt, qc_max_retries, prior) + "\n\n" + base_prompt
            (adir / "prompt.txt").write_text(prompt, encoding="utf-8")
            try:
                ref = [prev_strip] if (ci > 0 and prev_strip) else None
                result = run_codex_image_gen(
                    prompt=prompt,
                    baseline_image=None,           # text-to-sprite: no reference
                    workdir=adir,
                    extra_writable_dirs=[output_dir],
                    extra_reference_images=ref,
                )
                local_raw = adir / "raw-from-codex.png"
                shutil.copy2(result.raw_png_path, local_raw)
                proc = process_sheet(
                    local_raw, adir,
                    _process_opts(cfpi, frame_size_px, duration_ms,
                                  f"{animation_type}-c{ci + 1:02d}-frame",
                                  static=(template == "static_asset")),
                )
                if not proc.qc_warnings:
                    best = proc
                    break
                best = proc  # keep latest as fallback
                prior = list(proc.qc_warnings)
                log.warning("[%s] chunk %d attempt %d QC: %s",
                            label, ci + 1, attempt, " | ".join(proc.qc_warnings))
            except CodexInvocationError as exc:
                log.error("[%s] chunk %d attempt %d codex error: %s", label, ci + 1, attempt, exc)
                (adir / "error.txt").write_text(str(exc), encoding="utf-8")

        if best is None:
            return UnitOutcome(
                label=label, output_dir=output_dir, ok=False,
                elapsed_s=time.monotonic() - started,
                error=f"chunk {ci + 1}/{n_chunks} produced no usable output",
            )
        all_frames.extend(best.frame_paths)
        all_tframes.extend(best.transparent_frame_paths)
        qc_all.extend(best.qc_warnings)
        prev_strip = best.sheet
        offset += cfpi

    # Assemble final bundle.
    from .auto_rembg import save_transparent_gif
    from .postprocess import _save_opaque_gif

    final_dir = output_dir / "final"
    frames_dir = final_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = [Image.open(p).convert("RGBA") for p in all_frames]
    for i, fr in enumerate(frames, 1):
        fr.save(frames_dir / f"frame-{i:03d}.png")
    strip = final_dir / "strip.png"
    _hstrip(frames).save(strip)
    gif = final_dir / "animation.gif"
    _save_opaque_gif(frames, gif, duration_ms)

    tgif: Path | None = None
    if all_tframes and len(all_tframes) == len(all_frames):
        tframes = [Image.open(p).convert("RGBA") for p in all_tframes]
        tdir = final_dir / "transparent-frames"
        tdir.mkdir(parents=True, exist_ok=True)
        for i, fr in enumerate(tframes, 1):
            fr.save(tdir / f"frame-{i:03d}.png")
        _hstrip(tframes).save(final_dir / "transparent-strip.png")
        tgif = final_dir / "transparent.gif"
        save_transparent_gif(tframes, tgif, duration_ms)

    return UnitOutcome(
        label=label, output_dir=output_dir, ok=True,
        elapsed_s=time.monotonic() - started, frame_count=len(frames),
        strip=strip, gif=gif, transparent_gif=tgif, qc_warnings=qc_all,
    )


def _hstrip(frames: list) -> Any:
    from PIL import Image
    h = max(f.height for f in frames)
    total_w = sum(f.width for f in frames)
    canvas = Image.new("RGBA", (total_w, h), (0, 0, 0, 0))
    x = 0
    for f in frames:
        canvas.paste(f, (x, 0))
        x += f.width
    return canvas


# ───────────────────────────────── driver ───────────────────────────────────

def _character_units(c: CharacterSpec, root: Path, dry_run: bool) -> list[dict]:
    units = []
    for a in c.actions:
        units.append(dict(
            label=f"{c.id}/{a.action}",
            output_dir=root / c.id / a.action,
            template=c.template,
            animation_type=a.action,
            animation_details=a.notes,
            design=c.design,
            view=a.view or c.view,
            total_frames=a.frames,
            frame_size_px=c.frame_size_px,
            duration_ms=c.duration_ms,
            qc_max_retries=c.qc_max_retries,
            variations=[],
            asset_name="",
            dry_run=dry_run,
            art_style=c.art_style,
            style_prefix=c.style_prefix,
            style_block=c.style_block,
        ))
    return units


def _asset_unit(a: AssetSpec, root: Path, dry_run: bool) -> dict:
    # One panel per variation (capped at 4 for clean keying); 0 variations → single sprite.
    variations = a.variations[:MAX_PANELS_PER_CALL]
    total = max(1, len(variations))
    return dict(
        label=a.id,
        output_dir=root / a.id,
        template=a.template,
        animation_type=a.name or a.id,
        animation_details="",
        design=a.design,
        view="front",
        total_frames=total,
        frame_size_px=a.frame_size_px,
        duration_ms=120,
        qc_max_retries=a.qc_max_retries,
        variations=variations,
        asset_name=a.name or a.id,
        dry_run=dry_run,
        art_style=a.art_style,
        style_prefix=a.style_prefix,
        style_block=a.style_block,
    )


def run_pack(config: PackConfig, *, dry_run: bool = False, max_workers: int | None = None) -> list[UnitOutcome]:
    config.output_root.mkdir(parents=True, exist_ok=True)

    # Validate templates are text-to-sprite capable (we attach no reference).
    for c in config.characters:
        if c.template not in TEXT_TO_SPRITE_TEMPLATES:
            raise ValueError(
                f"character '{c.id}' uses template '{c.template}' which needs a reference image; "
                f"text-to-sprite templates are: {sorted(TEXT_TO_SPRITE_TEMPLATES)}"
            )
    for a in config.assets:
        if a.template not in TEXT_TO_SPRITE_TEMPLATES:
            raise ValueError(f"asset '{a.id}' uses non-text-to-sprite template '{a.template}'")

    units: list[dict] = []
    for c in config.characters:
        units.extend(_character_units(c, config.output_root, dry_run))
    for a in config.assets:
        units.append(_asset_unit(a, config.output_root, dry_run))

    if dry_run:
        # Sequential is fine; just writing prompt files.
        return [_run_unit_safe(u) for u in units]

    workers = max_workers or config.max_workers
    outcomes: list[UnitOutcome] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_unit_safe, u): u for u in units}
        for fut in as_completed(futures):
            outcomes.append(fut.result())
    return outcomes


def _run_unit_safe(unit: dict) -> UnitOutcome:
    try:
        return _generate_unit(**unit)
    except Exception as exc:  # noqa: BLE001 — one unit must not abort the pack
        return UnitOutcome(
            label=unit["label"], output_dir=unit["output_dir"], ok=False,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            dry_run=unit.get("dry_run", False),
        )

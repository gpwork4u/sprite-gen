"""sprite-gen CLI — server-free, drives codex CLI's built-in image_gen.

    python -m sprite_gen pack examples/pack.yaml --dry-run   # write prompts only
    python -m sprite_gen pack examples/pack.yaml             # real generation
    python -m sprite_gen process raw-sheet.png --cols 4 -o out/   # reprocess a sheet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pack import load_pack, run_pack
from .postprocess import ProcessOptions, process_sheet


def _cmd_pack(args: argparse.Namespace) -> int:
    config = load_pack(
        Path(args.spec),
        output_root=Path(args.output_root) if args.output_root else None,
    )
    n_actions = sum(len(c.actions) for c in config.characters)
    print(f"[sprite-gen] pack spec:   {args.spec}")
    print(f"[sprite-gen] output root: {config.output_root}")
    print(f"[sprite-gen] characters:  {len(config.characters)} ({n_actions} action strip(s))")
    print(f"[sprite-gen] assets:      {len(config.assets)}")
    print(f"[sprite-gen] mode:        {'DRY-RUN (prompts only)' if args.dry_run else 'REAL generation'}")
    print()

    outcomes = run_pack(config, dry_run=args.dry_run, max_workers=args.max_workers)

    print("=" * 64)
    print("RESULTS")
    print("=" * 64)
    fail = 0
    for o in outcomes:
        status = "OK  " if o.ok else "FAIL"
        if not o.ok:
            fail += 1
        flag = ""
        if o.ok and not o.dry_run and o.qc_warnings:
            flag = f"  (qc={len(o.qc_warnings)})"
        detail = f"{o.frame_count} frame(s)" if o.dry_run else f"{o.elapsed_s:>5.1f}s"
        print(f"  [{status}] {o.label:<28} {detail} -> {o.output_dir}{flag}")
        if not o.ok and o.error:
            print(f"         {o.error.splitlines()[0]}")
    print(f"\n{len(outcomes) - fail}/{len(outcomes)} unit(s) succeeded.")
    if args.dry_run:
        print(f"\n[dry-run] prompts written under {config.output_root}; codex was NOT called.")
    return 1 if fail else 0


def _cmd_process(args: argparse.Namespace) -> int:
    result = process_sheet(
        Path(args.input),
        Path(args.output_dir),
        ProcessOptions(
            rows=args.rows,
            cols=args.cols,
            chroma_key=False,
            also_auto_rembg=True,
            duration_ms=args.duration,
            label_prefix=args.label_prefix,
            output_frame_size=args.frame_size,
            normalize_per_frame_size=args.static,
        ),
    )
    print(f"output_dir: {result.output_dir}")
    print(f"sheet:      {result.sheet}")
    print(f"gif:        {result.animation_gif}")
    if result.qc_warnings:
        print(f"qc_warnings: {result.qc_warnings}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sprite-gen",
        description="Server-free text-to-sprite pixel-art generator on codex CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pack_p = sub.add_parser("pack", help="Generate an asset pack from a text-only YAML spec.")
    pack_p.add_argument("spec", help="Path to pack YAML (see examples/pack.yaml).")
    pack_p.add_argument("--output-root", default=None, help="Override output dir (default: from spec / .sprites).")
    pack_p.add_argument("--max-workers", type=int, default=None)
    pack_p.add_argument("--dry-run", action="store_true", help="Write prompts only; skip codex.")
    pack_p.set_defaults(func=_cmd_pack)

    proc_p = sub.add_parser("process", help="Slice / key / GIF an existing raw sprite sheet.")
    proc_p.add_argument("input", help="Raw sprite-sheet PNG.")
    proc_p.add_argument("-o", "--output-dir", required=True)
    proc_p.add_argument("--rows", type=int, default=1)
    proc_p.add_argument("--cols", type=int, required=True)
    proc_p.add_argument("--frame-size", type=int, default=384, help="Square output frame size.")
    proc_p.add_argument("--static", action="store_true",
                        help="Per-frame size normalization (uniform variation sheet). Off = shared scale.")
    proc_p.add_argument("--duration", type=int, default=120)
    proc_p.add_argument("--label-prefix", default="frame")
    proc_p.set_defaults(func=_cmd_process)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

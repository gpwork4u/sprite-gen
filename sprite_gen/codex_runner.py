"""Wrappers around `codex exec` for non-interactive use.

Two entry points:
- run_codex_image_gen: prompt + baseline -> codex calls image_gen, returns the
  PNG path. Used by the batch pipeline and web /generate.
- run_codex_json: prompt + image(s) + arbitrary output_schema -> codex returns
  a JSON object matching the schema. Used by /detect-identity for vision-only
  describe (no image_gen).

Both share the same subprocess plumbing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("pixelforge.codex")


def _codex_home() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home) if home else Path.home() / ".codex"


def _snapshot_generated_images() -> set[Path]:
    gen = _codex_home() / "generated_images"
    if not gen.exists():
        return set()
    return {p.resolve() for p in gen.rglob("*.png")}


_IMAGE_GEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["raw_png_path"],
    "properties": {
        "raw_png_path": {
            "type": "string",
            "description": "Absolute path to the PNG file produced by image_gen.",
        }
    },
}


@dataclass
class CodexResult:
    raw_png_path: Path
    stdout: str
    stderr: str


@dataclass
class CodexJsonResult:
    payload: dict[str, Any]
    stdout: str
    stderr: str


class CodexInvocationError(RuntimeError):
    pass


def _find_json_payload(stdout: str, required_key: str | None = None) -> dict[str, Any]:
    """Pull the last JSON object out of codex's stdout. Tries final line first,
    then scans the whole output for {...} blobs containing required_key (if given)."""
    tail = stdout.strip().splitlines()
    if tail:
        try:
            return json.loads(tail[-1])
        except json.JSONDecodeError:
            pass

    if required_key:
        pattern = re.compile(r"\{[^{}]*\"" + re.escape(required_key) + r"\"[^{}]*\}", re.S)
    else:
        pattern = re.compile(r"\{.*?\}", re.S)
    for raw in reversed(pattern.findall(stdout)):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue

    raise CodexInvocationError(
        f"could not parse codex JSON output. stdout tail:\n{stdout[-2000:]}"
    )


def _run_codex_subprocess(
    prompt: str,
    images: list[Path],
    *,
    workdir: Path,
    output_schema: dict[str, Any],
    model: str | None,
    extra_writable_dirs: list[Path] | None,
    timeout_seconds: int,
    sandbox: str,
    skip_git_repo_check: bool,
) -> subprocess.CompletedProcess[str]:
    workdir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix="-schema.json", delete=False, encoding="utf-8"
    ) as schema_file:
        json.dump(output_schema, schema_file)
        schema_path = Path(schema_file.name)

    cmd: list[str] = [
        "codex",
        "exec",
        "--cd",
        str(workdir),
        "--sandbox",
        sandbox,
        "--output-schema",
        str(schema_path),
        "--color",
        "never",
    ]
    for img in images:
        cmd.extend(["-i", str(img)])
    if skip_git_repo_check:
        cmd.append("--skip-git-repo-check")
    if model:
        cmd.extend(["-m", model])
    if extra_writable_dirs:
        for path in extra_writable_dirs:
            cmd.extend(["--add-dir", str(path)])
    cmd.append(prompt)

    env = dict(os.environ)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    finally:
        try:
            schema_path.unlink()
        except FileNotFoundError:
            pass


def run_codex_image_gen(
    prompt: str,
    baseline_image: Path | None = None,
    *,
    workdir: Path,
    model: str | None = None,
    extra_writable_dirs: list[Path] | None = None,
    extra_reference_images: list[Path] | None = None,
    timeout_seconds: int = 900,
    sandbox: str = "workspace-write",
    skip_git_repo_check: bool = True,
) -> CodexResult:
    """Run codex with an image_gen prompt and return the path of the generated PNG.

    `baseline_image` is OPTIONAL. When omitted (None), no reference image is
    attached and the model generates the sprite purely from the text prompt —
    this is the text-to-sprite path used for from-scratch asset generation.
    When provided, it is attached as the canonical identity reference.

    Codex sometimes reports a hallucinated path like /mnt/data/generated_image.png
    in its JSON output instead of the real on-disk file. Real PNGs from image_gen
    land under $CODEX_HOME/generated_images/<session-id>/ig_*.png. We snapshot
    that directory before and after the codex call, and prefer the new file we
    actually see on disk over whatever the model claimed.
    """
    before = _snapshot_generated_images()
    images: list[Path] = []
    if baseline_image is not None:
        images.append(baseline_image)
    if extra_reference_images:
        images.extend(extra_reference_images)
    proc = _run_codex_subprocess(
        prompt,
        images,
        workdir=workdir,
        output_schema=_IMAGE_GEN_SCHEMA,
        model=model,
        extra_writable_dirs=extra_writable_dirs,
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
        skip_git_repo_check=skip_git_repo_check,
    )
    after = _snapshot_generated_images()
    new_pngs = after - before

    if proc.returncode != 0:
        raise CodexInvocationError(
            f"codex exec exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-2000:]}\n--- stderr ---\n{proc.stderr[-2000:]}"
        )

    # Try the path the model reported first — sometimes it's right.
    reported_path: Path | None = None
    try:
        payload = _find_json_payload(proc.stdout, required_key="raw_png_path")
        rp = Path(payload["raw_png_path"]).expanduser()
        if not rp.is_absolute():
            rp = (workdir / rp).resolve()
        if rp.exists():
            reported_path = rp
    except CodexInvocationError:
        pass  # fall through to filesystem snapshot

    if reported_path is not None:
        return CodexResult(raw_png_path=reported_path, stdout=proc.stdout, stderr=proc.stderr)

    # Fall back to on-disk diff. Pick the most recently modified new PNG.
    if new_pngs:
        chosen = max(new_pngs, key=lambda p: p.stat().st_mtime)
        log.info(
            "codex reported path was missing; recovered from generated_images snapshot: %s",
            chosen,
        )
        return CodexResult(raw_png_path=chosen, stdout=proc.stdout, stderr=proc.stderr)

    raise CodexInvocationError(
        "image_gen produced no PNG. The model returned a path that does not exist "
        f"and no new files appeared under {_codex_home() / 'generated_images'}.\n"
        f"--- stdout ---\n{proc.stdout[-2500:]}\n--- stderr ---\n{proc.stderr[-1500:]}"
    )


def run_codex_json(
    prompt: str,
    images: list[Path],
    *,
    output_schema: dict[str, Any],
    workdir: Path,
    required_key: str | None = None,
    model: str | None = None,
    extra_writable_dirs: list[Path] | None = None,
    timeout_seconds: int = 300,
    sandbox: str = "read-only",
    skip_git_repo_check: bool = True,
) -> CodexJsonResult:
    """Run codex for vision/describe tasks. No image_gen, no file writes."""
    proc = _run_codex_subprocess(
        prompt,
        images,
        workdir=workdir,
        output_schema=output_schema,
        model=model,
        extra_writable_dirs=extra_writable_dirs,
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
        skip_git_repo_check=skip_git_repo_check,
    )
    if proc.returncode != 0:
        raise CodexInvocationError(
            f"codex exec exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-2000:]}\n--- stderr ---\n{proc.stderr[-2000:]}"
        )
    payload = _find_json_payload(proc.stdout, required_key=required_key)
    return CodexJsonResult(payload=payload, stdout=proc.stdout, stderr=proc.stderr)


# Back-compat alias for any external import; internal callers should use run_codex_image_gen.
run_codex = run_codex_image_gen

#!/usr/bin/env python3
"""Controlled adapter for AwakeFantasy's audited ImageClient, not its config/router."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from gateway import ConfigError, compatible, norm_base

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "references" / "components.json"


def load_client():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))["awakefantasy"]
    filename = (ROOT / spec["root"] / spec["client_file"]).resolve()
    if not filename.is_file():
        raise ConfigError("AwakeFantasy client is not present at the audited location; select official backend or restore the dependency")
    if hashlib.sha256(filename.read_bytes()).hexdigest() != spec["sha256"]:
        raise ConfigError("AwakeFantasy client changed since review; re-review before updating the pinned hash")
    module_spec = importlib.util.spec_from_file_location("imagegen_awakefantasy_client", filename)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module.ImageClient


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("generate", "edit"))
    p.add_argument("--model", required=True)
    prompt = p.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    p.add_argument("--image", action="append", default=[])
    p.add_argument("--size", default="auto")
    p.add_argument("--quality", default="auto", choices=("auto", "low", "medium", "high"))
    p.add_argument("--output-format", default="png", choices=("png", "jpeg", "webp"))
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, choices=(1,), default=1)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def plan(a):
    if not compatible(a.model):
        raise ConfigError("This adapter only supports discovered gpt-image-* models")
    if a.timeout <= 0 or a.timeout > 1800:
        raise ConfigError("Timeout must be in (0, 1800]")
    if a.size != "auto":
        import re
        match = re.fullmatch(r"([1-9][0-9]{0,4})x([1-9][0-9]{0,4})", a.size)
        if not match:
            raise ConfigError("Size must be auto or WIDTHxHEIGHT; endpoint support still needs verification")
    text = a.prompt if a.prompt is not None else Path(a.prompt_file).read_text(encoding="utf-8")
    if not text.strip():
        raise ConfigError("Empty prompt")
    if a.mode == "edit" and not a.image:
        raise ConfigError("Edit requires at least one --image")
    if a.mode == "generate" and a.image:
        raise ConfigError("Reference images require edit mode")
    for image in a.image:
        if not Path(image).is_file():
            raise ConfigError("Input image not found")
    out = Path(a.out).resolve()
    if out.suffix.lower() not in {"png": (".png",), "jpeg": (".jpg", ".jpeg"), "webp": (".webp",)}[a.output_format]:
        raise ConfigError("Output suffix must match --output-format")
    sidecar = out.with_suffix(".meta.json")
    for path in (out, sidecar):
        if path.exists() and (not a.force or not path.is_file()):
            raise ConfigError("Output or metadata already exists; choose a new filename or explicitly --force")
    return text, out, sidecar


def run(a):
    text, out, sidecar = plan(a)
    # Verify/import audited upstream implementation even for dry-run, but never create a client.
    client_type = load_client()
    if a.dry_run:
        print(json.dumps({"backend": "AwakeFantasy/relay-imagegen ImageClient", "model": a.model,
                          "outputs": [str(out), str(sidecar)], "size": a.size, "quality": a.quality,
                          "note": "Offline only; model/size capability is not verified"}, ensure_ascii=False))
        return 0
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ConfigError("Use relay_gen.py to supply a matched key/base pair")
    base = norm_base(os.environ.get("OPENAI_BASE_URL", ""))
    if key in text or key in str(out):
        raise ConfigError("Do not include the API key in prompts or output paths")
    import httpx
    client = client_type(api_key=key, base_url=base, timeout=a.timeout)
    # No SDK retries, cross-provider failover or credential-bearing redirects.
    original = client.client
    transport = httpx.Client(timeout=a.timeout, follow_redirects=False)
    client.client = original.with_options(max_retries=0, http_client=transport)
    try:
        params = dict(model=a.model, prompt=text, size=a.size, quality=a.quality, output_format=a.output_format)
        if a.mode == "edit":
            images = client.edit(**params, image_paths=a.image)
        else:
            images = client.generate(**params)
    finally:
        client.client.close()
        original.close()
    if not images or len(images) != 1:
        raise ConfigError("Expected one completed image; request may already have been charged, do not automatically retry")
    from PIL import Image
    image = Image.open(io.BytesIO(images[0]))
    image.load()
    dimensions = list(image.size)
    actual_format = image.format
    expected = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}[a.output_format]
    if actual_format != expected:
        raise ConfigError("Relay returned another image format; no mislabeled file was saved; do not automatically retry")
    metadata = {"backend": "AwakeFantasy/relay-imagegen ImageClient", "mode": a.mode, "model": a.model,
                "requested_size": a.size, "actual_dimensions": dimensions, "quality": a.quality,
                "output": str(out), "prompt_snapshot": text, "reference_images": a.image,
                "note": "Local metadata contains the user prompt but no API key or gateway URL"}
    out.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive writes protect existing files unless the user explicitly requested overwrite.
    with out.open("wb" if a.force else "xb") as f:
        f.write(images[0])
    with sidecar.open("w" if a.force else "x", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(json.dumps({"status": "completed", "outputs": [str(out)], "metadata": str(sidecar), "actual_dimensions": dimensions}, ensure_ascii=False))
    return 0


def main(argv=None):
    return run(parse(argv))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        key = os.environ.get("OPENAI_API_KEY", "")
        message = str(exc).replace(key, "[REDACTED]") if key else str(exc)
        print(json.dumps({"status": "awake_backend_failed", "message": message,
                          "next_action": "inspect_error_no_automatic_retry"}, ensure_ascii=False))
        raise SystemExit(2)

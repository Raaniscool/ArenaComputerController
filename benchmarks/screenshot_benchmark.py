#!/usr/bin/env python3
"""Reproducible local image-encoding benchmark for desktop screenshots.

No image is uploaded. Provider latency/billing are intentionally not guessed;
this script measures local representation cost and can estimate wire time for a
caller-supplied link rate.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _load_pillow():
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise RuntimeError("Pillow is required. Install it with: python -m pip install -e '.[benchmark]'") from exc
    return Image, ImageChops, ImageStat


def _capture_desktop():
    """Capture with available local backends; prefer MSS but time ImageGrab too."""
    Image, _, _ = _load_pillow()
    captures = []
    chosen_image = None

    try:
        import mss

        start = time.perf_counter()
        with mss.mss() as screen:
            monitor = screen.monitors[0]
            shot = screen.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        captures.append({"method": "mss", "available": True, "capture_ms": round(elapsed_ms, 4), "image": image})
        chosen_image = image
    except Exception as exc:
        captures.append({"method": "mss", "available": False, "unavailable_reason": type(exc).__name__})

    try:
        from PIL import ImageGrab

        start = time.perf_counter()
        image = ImageGrab.grab(all_screens=True).convert("RGB")
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        captures.append(
            {"method": "pillow_imagegrab", "available": True, "capture_ms": round(elapsed_ms, 4), "image": image}
        )
        if chosen_image is None:
            chosen_image = image
    except Exception as exc:
        captures.append({"method": "pillow_imagegrab", "available": False, "unavailable_reason": type(exc).__name__})

    if chosen_image is None:
        raise RuntimeError("No screenshot capture backend succeeded; install mss and Pillow and run in a desktop session.")

    public_results = []
    for result in captures:
        public = {key: value for key, value in result.items() if key != "image"}
        sample = result.get("image")
        if sample is not None:
            public["width"], public["height"] = sample.size
            if sample.size == chosen_image.size and sample is not chosen_image:
                public["comparison_to_primary"] = _measure_error(chosen_image, sample)
        public_results.append(public)
    return chosen_image, public_results


def _load_image(path: str):
    Image, _, _ = _load_pillow()
    with Image.open(path) as opened:
        return opened.convert("RGB")


def _save_image(image, fmt: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _decode_image(data: bytes):
    Image, _, _ = _load_pillow()
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        return opened.convert("RGB")


def _measure_error(original, decoded) -> dict:
    _, ImageChops, ImageStat = _load_pillow()
    if decoded.size != original.size:
        return {"mae": None, "rmse": None, "psnr_db": None, "exact": False}
    difference = ImageChops.difference(original, decoded)
    stats = ImageStat.Stat(difference)
    mae = sum(stats.mean) / max(1, len(stats.mean))
    rmse = math.sqrt(sum(value * value for value in stats.rms) / max(1, len(stats.rms)))
    psnr = None if rmse == 0 else 20.0 * math.log10(255.0 / rmse)
    return {
        "mae": round(mae, 6),
        "rmse": round(rmse, 6),
        "psnr_db": None if psnr is None else round(psnr, 4),
        "exact": original.tobytes() == decoded.tobytes(),
    }


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples), 4)


def _benchmark_method(name: str, image, encode: Callable[[], bytes], decode: Callable[[bytes], object], repeats: int, link_mbps: float | None) -> dict:
    encoded_samples: list[bytes] = []
    encode_times: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        value = encode()
        encode_times.append((time.perf_counter() - started) * 1000.0)
        encoded_samples.append(value)

    encoded = encoded_samples[-1]
    decoded_samples = []
    decode_times: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        decoded = decode(encoded)
        decode_times.append((time.perf_counter() - started) * 1000.0)
        decoded_samples.append(decoded)

    accuracy = _measure_error(image, decoded_samples[-1])
    is_text = name in {"base64_rgb", "base64_png", "pixel_hex_rgb"}
    text_chars = len(encoded) if is_text else None
    transfer_ms = None
    if link_mbps is not None:
        transfer_ms = round(len(encoded) * 8 / (link_mbps * 1_000_000) * 1000.0, 4)

    return {
        "method": name,
        "encoded_bytes": len(encoded),
        "base64_expansion_bytes": max(0, len(encoded) - len(encoded) * 3 // 4) if name.startswith("base64_") else None,
        "encode_median_ms": _median_ms(encode_times),
        "decode_median_ms": _median_ms(decode_times),
        "text_characters": text_chars,
        "rough_text_tokens_at_four_chars_each": math.ceil(text_chars / 4) if text_chars is not None else None,
        "estimated_transfer_ms_at_link_rate": transfer_ms,
        "reconstruction": accuracy,
    }


def _build_methods(image, repeats: int, link_mbps: float | None) -> list[dict]:
    Image, _, _ = _load_pillow()
    methods = []
    raw = image.tobytes()

    methods.append(
        _benchmark_method(
            "raw_rgb",
            image,
            lambda: image.tobytes(),
            lambda data: Image.frombytes("RGB", image.size, data),
            repeats,
            link_mbps,
        )
    )
    methods.append(
        _benchmark_method(
            "base64_rgb",
            image,
            lambda: base64.b64encode(image.tobytes()),
            lambda data: Image.frombytes("RGB", image.size, base64.b64decode(data)),
            repeats,
            link_mbps,
        )
    )
    methods.append(
        _benchmark_method(
            "pixel_hex_rgb",
            image,
            lambda: image.tobytes().hex().encode("ascii"),
            lambda data: Image.frombytes("RGB", image.size, bytes.fromhex(data.decode("ascii"))),
            repeats,
            link_mbps,
        )
    )

    for level in (1, 6, 9):
        methods.append(
            _benchmark_method(
                f"png_level_{level}",
                image,
                lambda level=level: _save_image(image, "PNG", compress_level=level, optimize=False),
                _decode_image,
                repeats,
                link_mbps,
            )
        )

    methods.append(
        _benchmark_method(
            "base64_png_level_1",
            image,
            lambda: base64.b64encode(_save_image(image, "PNG", compress_level=1, optimize=False)),
            lambda data: _decode_image(base64.b64decode(data)),
            repeats,
            link_mbps,
        )
    )

    extensions = Image.registered_extensions()
    for quality in (60, 80, 92):
        methods.append(
            _benchmark_method(
                f"jpeg_q{quality}",
                image,
                lambda quality=quality: _save_image(
                    image, "JPEG", quality=quality, subsampling=1, optimize=False, progressive=False
                ),
                _decode_image,
                repeats,
                link_mbps,
            )
        )
        if extensions.get(".webp") == "WEBP":
            methods.append(
                _benchmark_method(
                    f"webp_q{quality}",
                    image,
                    lambda quality=quality: _save_image(image, "WEBP", quality=quality, method=0),
                    _decode_image,
                    repeats,
                    link_mbps,
                )
            )

    # AVIF is tested when the active Pillow build has an encoder; it is not a hard dependency.
    if extensions.get(".avif") == "AVIF":
        try:
            methods.append(
                _benchmark_method(
                    "avif_q60",
                    image,
                    lambda: _save_image(image, "AVIF", quality=60),
                    _decode_image,
                    repeats,
                    link_mbps,
                )
            )
        except Exception as exc:
            methods.append({"method": "avif_q60", "available": False, "unavailable_reason": type(exc).__name__})

    # A retained reference makes it explicit that the unencoded pixel payload is 3 bytes/pixel.
    assert len(raw) == image.width * image.height * 3
    return methods


def _benchmark_difference(previous, current, link_mbps: float | None) -> dict:
    Image, ImageChops, _ = _load_pillow()
    if previous.size != current.size:
        return {"available": False, "reason": "Previous and current images have different dimensions."}
    bounds = ImageChops.difference(previous, current).getbbox()
    full_png = _save_image(current, "PNG", compress_level=1, optimize=False)
    if bounds is None:
        return {
            "available": True,
            "changed": False,
            "changed_bounds": None,
            "changed_bounds_area_ratio": 0.0,
            "patch_png_bytes": 0,
            "full_png_bytes": len(full_png),
            "reconstruction_exact": True,
            "estimated_transfer_ms_at_link_rate": 0.0 if link_mbps is not None else None,
            "note": "An unchanged frame needs only a small protocol header; this benchmark reports image payload bytes.",
        }

    patch = current.crop(bounds)
    patch_png = _save_image(patch, "PNG", compress_level=1, optimize=False)
    reconstructed = previous.copy()
    reconstructed.paste(_decode_image(patch_png), bounds[:2])
    ratio = (patch.width * patch.height) / (current.width * current.height)
    patch_bytes = len(patch_png)
    transfer_ms = None if link_mbps is None else round(patch_bytes * 8 / (link_mbps * 1_000_000) * 1000.0, 4)
    return {
        "available": True,
        "changed": True,
        "changed_bounds": {"x": bounds[0], "y": bounds[1], "width": patch.width, "height": patch.height},
        "changed_bounds_area_ratio": round(ratio, 6),
        "patch_png_bytes": patch_bytes,
        "full_png_bytes": len(full_png),
        "reconstruction_exact": reconstructed.tobytes() == current.tobytes(),
        "estimated_transfer_ms_at_link_rate": transfer_ms,
        "note": "A single bounding-box patch is measured; multiple disjoint rectangles may be smaller. A vision API may still require a reconstructed full frame.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="Screenshot image to benchmark")
    parser.add_argument("--capture", action="store_true", help="Capture the current desktop via MSS instead of reading an image")
    parser.add_argument("--previous", help="Previous screenshot for changed-region benchmarking")
    parser.add_argument("--link-mbps", type=float, help="Estimate transfer time at this link speed (Mbps); no transfer is performed")
    parser.add_argument("--repeats", type=int, default=3, help="Encode/decode repetitions per method (default: 3)")
    parser.add_argument("--output", help="Write JSON results to a file instead of stdout")
    args = parser.parse_args(argv)

    if args.capture == bool(args.image):
        parser.error("Provide exactly one of IMAGE or --capture")
    if args.repeats < 1 or args.repeats > 20:
        parser.error("--repeats must be between 1 and 20")
    if args.link_mbps is not None and args.link_mbps <= 0:
        parser.error("--link-mbps must be greater than zero")

    try:
        capture_benchmarks = None
        capture_ms = None
        capture_backend = None
        if args.capture:
            image, capture_benchmarks = _capture_desktop()
            chosen_capture = next(
                (item for item in capture_benchmarks if item.get("available")), None
            )
            capture_ms = chosen_capture.get("capture_ms") if chosen_capture else None
            capture_backend = chosen_capture.get("method") if chosen_capture else None
            source = "live desktop capture"
        else:
            image = _load_image(args.image)
            source = str(Path(args.image).resolve())
        previous = _load_image(args.previous) if args.previous else None
        methods = _build_methods(image, args.repeats, args.link_mbps)
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "capture_ms": None if capture_ms is None else round(capture_ms, 4),
            "capture_backend": capture_backend,
            "capture_backends": capture_benchmarks,
            "image": {"width": image.width, "height": image.height, "pixels": image.width * image.height},
            "link_mbps_for_estimates": args.link_mbps,
            "methods": methods,
            "differential": _benchmark_difference(previous, image, args.link_mbps) if previous is not None else None,
            "limitations": [
                "No image was sent over a network and no AI model was called.",
                "Text token count is a rough character-count estimate, not provider tokenizer output or billed tokens.",
                "AI processing latency and provider-side image resizing are not measured.",
                "Differential mode encodes one changed bounding box; the receiving model may need a locally reconstructed full image.",
                "Back-to-back capture-method image comparisons can differ if the visible desktop changes during capture.",
            ],
        }
    except Exception as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 2

    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

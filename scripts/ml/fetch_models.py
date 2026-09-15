"""Downloads pretrained ONNX models for the AI building-tile pipeline.

Presets:
  * hot-buildings     HOT fAIr DINOv3-S building footprints (aerial, recommended)
  * geobase-unet      GeoBase U-Net building footprints (lightweight)
  * ade20k-segformer  ADE20K SegFormer-B0 (generic scene parsing)
  * depth             Depth Anything V2 Small (heights)

Usage:
    python -m scripts.ml.fetch_models
    python -m scripts.ml.fetch_models --presets hot-buildings,geobase-unet --quantized
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .presets import (
    DEFAULT_SEGMENTER,
    DEPTH_FILE,
    DEPTH_FILE_QUANTIZED,
    DEPTH_REPO,
    DEPTH_TARGET,
    SEGMENTER_PRESETS,
)

DEFAULT_MODELS_DIR = Path(__file__).resolve().parent / "models"

SAM3_REPO = "danilobukvic/sam3-text-onnx"
SAM3_FILES = [
    ("vision_encoder_int8.onnx", "vision_encoder_int8.onnx"),
    ("text_encoder_int8.onnx", "text_encoder_int8.onnx"),
    ("decoder_int8.onnx", "decoder_int8.onnx"),
    ("tokenizer.json", "tokenizer.json"),
    ("tokenizer_config.json", "tokenizer_config.json"),
]


def download_file(repo_id: str, filename: str, target: Path) -> Path:
    from huggingface_hub import hf_hub_download

    cached = hf_hub_download(repo_id=repo_id, filename=filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, target)
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"  {repo_id}/{filename} -> {target} ({size_mb:.1f} MB)")
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ml.fetch_models",
        description="Download building-segmentation and depth ONNX weights.",
    )
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    parser.add_argument(
        "--presets",
        default=DEFAULT_SEGMENTER,
        help=f"comma separated segmenter presets (default: {DEFAULT_SEGMENTER})",
    )
    parser.add_argument("--quantized", action="store_true", help="use int8 variants when available")
    parser.add_argument("--skip-depth", action="store_true")
    parser.add_argument("--list", action="store_true", help="list available presets and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        for key, preset in SEGMENTER_PRESETS.items():
            print(f"  {key:18s} {preset.label} (~{preset.size_hint_mb:.0f} MB)")
        print(f"  {'sam3':18s} SAM3 text-promptable roof refinement (~880 MB)")
        return 0

    models_dir: Path = args.models_dir
    print(f"Downloading ONNX models into {models_dir}")

    for key in [part.strip() for part in args.presets.split(",") if part.strip()]:
        if key == "sam3":
            target_dir = models_dir / "sam3"
            for source, target in SAM3_FILES:
                download_file(SAM3_REPO, source, target_dir / target)
            continue
        preset = SEGMENTER_PRESETS.get(key)
        if preset is None:
            print(f"  warning: unknown preset '{key}', skipping")
            continue
        filename = preset.file
        if args.quantized and preset.quantized_file:
            filename = preset.quantized_file
        download_file(preset.repo, filename, models_dir / preset.target)

    if not args.skip_depth:
        depth_file = DEPTH_FILE_QUANTIZED if args.quantized else DEPTH_FILE
        download_file(DEPTH_REPO, depth_file, models_dir / DEPTH_TARGET)

    print("Done. Run the pipeline with:")
    print(
        "  python -m scripts.ml.pipeline --bbox <min_lon,min_lat,max_lon,max_lat> "
        "--zooms 16 --heights"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

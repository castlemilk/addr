"""Pretrained model presets shared by fetch_models and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SegmenterPreset:
    key: str
    label: str
    repo: str
    file: str
    target: str
    input_size: int
    layout: str
    normalization: str
    output_mode: str
    activation: str
    threshold: float
    building_classes: tuple[int, ...] = ()
    stride: int = 0
    quantized_file: str | None = None
    size_hint_mb: float = 0.0


SEGMENTER_PRESETS: dict[str, SegmenterPreset] = {
    "hot-buildings": SegmenterPreset(
        key="hot-buildings",
        label="HOT fAIr DINOv3-S building footprints (aerial, recommended)",
        repo="hotosm/dinov3s-buildings",
        file="model.onnx",
        target="dinov3s_buildings.onnx",
        input_size=256,
        layout="nchw",
        normalization="unit",
        output_mode="binary",
        activation="sigmoid",
        threshold=0.4371,
        stride=192,
        size_hint_mb=219.6,
    ),
    "geobase-unet": SegmenterPreset(
        key="geobase-unet",
        label="GeoBase U-Net building footprints (lightweight, 30 MB)",
        repo="geobase/building-footprint-segmentation",
        file="onnx/model.onnx",
        target="geobase_unet_buildings.onnx",
        input_size=256,
        layout="nhwc",
        normalization="imagenet",
        output_mode="binary",
        activation="auto",
        threshold=0.5,
        quantized_file="onnx/model_quantized.onnx",
        size_hint_mb=29.9,
    ),
    "ade20k-segformer": SegmenterPreset(
        key="ade20k-segformer",
        label="ADE20K SegFormer-B0 (generic classes, outlines only)",
        repo="Xenova/segformer-b0-finetuned-ade-512-512",
        file="onnx/model.onnx",
        target="segformer_b0_ade.onnx",
        input_size=512,
        layout="nchw",
        normalization="imagenet",
        output_mode="multiclass",
        activation="auto",
        threshold=0.5,
        building_classes=(1, 25),
        quantized_file="onnx/model_quantized.onnx",
        size_hint_mb=14.6,
    ),
}

DEFAULT_SEGMENTER = "hot-buildings"

DEPTH_REPO = "onnx-community/depth-anything-v2-small"
DEPTH_FILE = "onnx/model.onnx"
DEPTH_FILE_QUANTIZED = "onnx/model_quantized.onnx"
DEPTH_TARGET = "depth_anything_v2_small.onnx"


def get_preset(key: str) -> SegmenterPreset:
    try:
        return SEGMENTER_PRESETS[key]
    except KeyError as error:
        choices = ", ".join(SEGMENTER_PRESETS)
        raise ValueError(f"unknown segmenter preset '{key}' (choose from: {choices})") from error

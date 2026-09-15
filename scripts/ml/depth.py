"""Monocular relative depth estimation (Depth Anything V2) and per-building height
assignment from roof-vs-ground depth differences.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image
from shapely.geometry import Polygon

from .segmentation import IMAGENET_MEAN, IMAGENET_STD, sample_grid

MIN_HEIGHT_M = 2.5
MAX_HEIGHT_M = 150.0


@dataclass
class DepthStats:
    backend: str
    input_size: int


class OnnxDepthEstimator:
    """Runs a monocular depth model and returns a normalized 0..1 elevation proxy."""

    def __init__(
        self,
        model_path: str | Path,
        input_size: int = 518,
        providers: list[str] | None = None,
    ):
        import onnxruntime as ort

        self.model_path = str(model_path)
        self.input_size = input_size
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            self.model_path,
            sess_options=options,
            providers=providers or ort.get_available_providers(),
        )
        self.input_name = self.session.get_inputs()[0].name
        self.stats = DepthStats("onnx", input_size)

    def estimate(self, image: Image.Image) -> np.ndarray:
        rgb = image.convert("RGB")
        width, height = rgb.size
        resized = rgb.resize(
            (self.input_size, self.input_size), Image.Resampling.BILINEAR
        )
        array = np.asarray(resized, dtype=np.float32) / 255.0
        array = (array - IMAGENET_MEAN) / IMAGENET_STD
        tensor = np.transpose(array, (2, 0, 1))[None]
        output = np.asarray(self.session.run(None, {self.input_name: tensor})[0])
        while output.ndim > 2 and output.shape[0] == 1:
            output = output[0]
        if output.ndim != 2:
            raise ValueError(f"unexpected depth output shape: {output.shape}")

        if output.shape != (height, width):
            output = np.asarray(
                Image.fromarray(output.astype(np.float32), mode="F").resize(
                    (width, height), Image.Resampling.BILINEAR
                ),
                dtype=np.float32,
            )

        minimum = float(np.min(output))
        maximum = float(np.max(output))
        if maximum - minimum < 1e-6:
            return np.zeros_like(output, dtype=np.float32)
        return (output - minimum) / (maximum - minimum)


def _sample_depth(
    geometry: Polygon, depth: np.ndarray, step: float = 2.0
) -> np.ndarray:
    from shapely import contains_xy

    points = sample_grid(geometry, step=step)
    if points.size == 0:
        return np.empty((0,), dtype=np.float32)
    xs = points[:, 0]
    ys = points[:, 1]
    inside = contains_xy(geometry, xs, ys)
    if not bool(np.any(inside)):
        representative = geometry.representative_point()
        px = int(round(representative.x))
        py = int(round(representative.y))
        if 0 <= py < depth.shape[0] and 0 <= px < depth.shape[1]:
            return np.asarray([depth[py, px]], dtype=np.float32)
        return np.empty((0,), dtype=np.float32)
    px = np.clip(xs[inside].astype(np.int64), 0, depth.shape[1] - 1)
    py = np.clip(ys[inside].astype(np.int64), 0, depth.shape[0] - 1)
    return depth[py, px]


def _disparities(
    polygons: list[Polygon],
    depth: np.ndarray,
    min_diff: float,
    sample_step: float,
) -> list[float | None]:
    raw: list[float | None] = []
    for polygon in polygons:
        roof_geom = polygon.buffer(-1.5)
        if roof_geom.is_empty or not isinstance(roof_geom, Polygon):
            roof_geom = polygon
        ring_geom = polygon.buffer(6.0).difference(polygon.buffer(1.5))
        if not isinstance(ring_geom, Polygon):
            ring_geom = polygon.buffer(6.0)
        roof_values = _sample_depth(cast(Polygon, roof_geom), depth, sample_step)
        ground_values = _sample_depth(cast(Polygon, ring_geom), depth, sample_step)
        if roof_values.size == 0 or ground_values.size < 3:
            raw.append(None)
            continue
        roof = float(np.percentile(roof_values, 60))
        ground = float(np.percentile(ground_values, 40))
        diff = roof - ground
        raw.append(diff if diff > min_diff else None)
    return raw


def estimate_meters_per_unit(
    polygons: list[Polygon],
    depth: np.ndarray,
    assume_height: float = 7.0,
    min_diff: float = 0.01,
    sample_step: float = 2.0,
) -> float | None:
    """Anchors the median roof-vs-ground disparity to an assumed real-world height."""
    disparities = _disparities(polygons, depth, min_diff, sample_step)
    positive = [value for value in disparities if value is not None]
    if not positive:
        return None
    return assume_height / float(np.median(positive))


def estimate_heights(
    polygons: list[Polygon],
    depth: np.ndarray,
    assume_height: float | None = 7.0,
    meters_per_unit: float | None = None,
    min_diff: float = 0.01,
    sample_step: float = 2.0,
) -> list[float | None]:
    """Derives metric heights from relative depth disparities.

    Height is sampled from the polygon interior (roof/canopy) against an outer
    ground ring. Without an explicit metric scale the median disparity across
    `polygons` is anchored to `assume_height`; pass `meters_per_unit` to share
    one calibration across multiple geometry classes (e.g. buildings + trees).
    """
    raw = _disparities(polygons, depth, min_diff, sample_step)
    positive = [value for value in raw if value is not None]
    if not positive:
        return [None] * len(polygons)

    if meters_per_unit is not None:
        factor = meters_per_unit
    elif assume_height is not None:
        factor = assume_height / float(np.median(positive))
    else:
        factor = 1.0

    heights: list[float | None] = []
    for value in raw:
        if value is None:
            heights.append(None)
            continue
        height = float(np.clip(value * factor, MIN_HEIGHT_M, MAX_HEIGHT_M))
        heights.append(round(height, 1))
    return heights


def depth_terrain_prior(depth: np.ndarray, percentile: float = 15.0) -> float:
    """Estimates the ground-level depth value of a scene."""
    if depth.size == 0:
        return 0.0
    return float(np.percentile(depth, percentile))


def depth_to_rgb(depth: np.ndarray) -> Image.Image:
    """Colorizes a normalized depth map with a turbo-like ramp for previews."""
    t = np.clip(depth, 0.0, 1.0)
    red = np.clip(1.5 - np.abs(4.0 * t - 3.0), 0.0, 1.0)
    green = np.clip(1.5 - np.abs(4.0 * t - 2.0), 0.0, 1.0)
    blue = np.clip(1.5 - np.abs(4.0 * t - 1.0), 0.0, 1.0)
    rgb = np.stack((red, green, blue), axis=-1)
    return Image.fromarray((rgb * 255.0).astype(np.uint8), mode="RGB")


def height_sort_key(height: float | None) -> float:
    if height is None:
        return 0.0
    if math.isnan(height):
        return 0.0
    return height

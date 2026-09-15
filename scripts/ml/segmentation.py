"""Building footprint segmentation: ONNX SegFormer inference with an offline fallback,
plus mask-to-polygon vectorization.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image
from shapely.geometry import Polygon, box
from shapely.ops import transform, unary_union
from shapely.validation import make_valid

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _softmax(logits: np.ndarray, axis: int = 0) -> np.ndarray:
    shifted = logits - np.max(logits, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=axis, keepdims=True)


def _morph(mask: np.ndarray, radius: int, mode: str) -> np.ndarray:
    if radius <= 0:
        return mask
    padded = np.pad(mask, radius, mode="edge")
    accumulator = None
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            window = padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
            if accumulator is None:
                accumulator = window.copy()
            elif mode == "dilate":
                accumulator |= window
            else:
                accumulator &= window
    return accumulator if accumulator is not None else mask


def binary_dilate(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    result = np.asarray(mask, dtype=bool)
    for _ in range(iterations):
        result = _morph(result, 1, "dilate")
    return result


def binary_erode(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    result = np.asarray(mask, dtype=bool)
    for _ in range(iterations):
        result = _morph(result, 1, "erode")
    return result


def binary_close(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    return binary_erode(binary_dilate(mask, iterations), iterations)


def binary_open(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    return binary_dilate(binary_erode(mask, iterations), iterations)


def iter_polygons(geometry) -> Iterator[Polygon]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield cast(Polygon, geometry)
    elif geometry.geom_type == "MultiPolygon":
        for part in geometry.geoms:
            yield cast(Polygon, part)
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from iter_polygons(part)


def mask_to_polygons(
    mask: np.ndarray, min_area_px: float = 48.0, simplify_px: float = 1.0
) -> list[Polygon]:
    """Vectorizes a boolean building mask using run-length rectangles + polygon union."""
    mask = np.asarray(mask, dtype=bool)
    rectangles = []
    height = mask.shape[0]
    for y in range(height):
        row = mask[y]
        if not row.any():
            continue
        changes = np.diff(row.astype(np.int8), prepend=0, append=0)
        starts = np.flatnonzero(changes == 1)
        ends = np.flatnonzero(changes == -1)
        for x0, x1 in zip(starts, ends):
            rectangles.append(box(float(x0), float(y), float(x1), float(y + 1)))

    if not rectangles:
        return []

    merged = unary_union(rectangles)
    if not merged.is_valid:
        merged = make_valid(merged)
    if simplify_px > 0:
        candidate = merged.simplify(simplify_px, preserve_topology=True)
        if not candidate.is_empty:
            merged = candidate

    polygons: list[Polygon] = []
    for geom in iter_polygons(merged):
        if not geom.is_valid:
            geom = make_valid(geom)
        for part in iter_polygons(geom):
            if part.area >= min_area_px:
                polygons.append(part)
    return polygons


@dataclass
class SegmenterStats:
    backend: str
    model_path: str
    input_size: int
    threshold: float
    stride: int
    normalization: str
    output_mode: str


NORMALIZATION_STATS = {
    "imagenet": (
        np.array([0.485, 0.456, 0.406], dtype=np.float32),
        np.array([0.229, 0.224, 0.225], dtype=np.float32),
    ),
    "hot": (
        np.array([0.4297, 0.4002, 0.3433], dtype=np.float32),
        np.array([0.2056, 0.1674, 0.1599], dtype=np.float32),
    ),
}


class OnnxBuildingSegmenter:
    """Runs a building segmentation ONNX model with optional full-resolution sliding window.

    Supports multi-class scene-parsing models (softmax over building classes) and
    binary building-probability models (mask logit + sigmoid), in NCHW or NHWC layouts.
    """

    def __init__(
        self,
        model_path: str | Path,
        building_classes: tuple[int, ...] = (1, 25),
        input_size: int = 512,
        threshold: float = 0.5,
        normalization: str = "imagenet",
        output_mode: str = "multiclass",
        activation: str = "auto",
        layout: str = "nchw",
        stride: int | None = None,
        close_iterations: int = 1,
        providers: list[str] | None = None,
    ):
        import onnxruntime as ort

        self.model_path = str(model_path)
        self.building_classes = tuple(building_classes)
        self.threshold = threshold
        self.normalization = normalization
        self.output_mode = output_mode
        self.activation = activation
        self.layout = layout
        self.close_iterations = close_iterations
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            self.model_path,
            sess_options=options,
            providers=providers or ort.get_available_providers(),
        )
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.input_size = _static_input_size(shape, input_size, layout)
        self.stride = stride if stride and stride > 0 else max(1, int(self.input_size * 0.75))
        self.stats = SegmenterStats(
            backend="onnx",
            model_path=self.model_path,
            input_size=self.input_size,
            threshold=threshold,
            stride=self.stride,
            normalization=normalization,
            output_mode=output_mode,
        )

    def _preprocess(self, image: Image.Image) -> np.ndarray:
        array = np.asarray(image.convert("RGB"), dtype=np.float32)
        if self.normalization == "unit":
            array = array / 255.0
        elif self.normalization in NORMALIZATION_STATS:
            mean, std = NORMALIZATION_STATS[self.normalization]
            array = (array / 255.0 - mean) / std
        if self.layout == "nchw":
            return np.transpose(array, (2, 0, 1))[None]
        return array[None]

    def _probability(self, output: np.ndarray) -> np.ndarray:
        array = np.asarray(output, dtype=np.float32)
        while array.ndim > 3 and array.shape[0] == 1:
            array = array[0]
        if self.output_mode == "multiclass":
            if array.ndim != 3:
                raise ValueError(f"unexpected segmentation output shape: {output.shape}")
            probs = _softmax(array, axis=0)
            selected = np.zeros(probs.shape[1:], dtype=np.float32)
            for index in self.building_classes:
                if 0 <= index < probs.shape[0]:
                    selected += probs[index]
            return selected

        if self.layout == "nchw":
            candidate = array[0] if array.ndim == 3 else array
        else:
            candidate = array[..., 0] if array.ndim == 3 else array
        if self.activation == "sigmoid":
            return 1.0 / (1.0 + np.exp(-candidate))
        if self.activation == "none":
            return candidate
        if float(candidate.min()) >= 0.0 and float(candidate.max()) <= 1.0:
            return candidate
        return 1.0 / (1.0 + np.exp(-candidate))

    def _infer(self, image: Image.Image) -> np.ndarray:
        tensor = self._preprocess(image)
        output = self.session.run(None, {self.input_name: tensor})[0]
        return self._probability(output)

    def segment(self, image: Image.Image) -> np.ndarray:
        rgb = image.convert("RGB")
        width, height = rgb.size
        size = self.input_size

        if width < size or height < size:
            resized = rgb.resize((size, size), Image.Resampling.BILINEAR)
            probability = self._infer(resized)
            probability = np.asarray(
                Image.fromarray(probability.astype(np.float32), mode="F").resize(
                    (width, height), Image.Resampling.BILINEAR
                ),
                dtype=np.float32,
            )
        else:
            probability = np.zeros((height, width), dtype=np.float32)
            counts = np.zeros((height, width), dtype=np.float32)
            xs = list(range(0, width - size + 1, self.stride))
            if xs[-1] != width - size:
                xs.append(width - size)
            ys = list(range(0, height - size + 1, self.stride))
            if ys[-1] != height - size:
                ys.append(height - size)
            for y0 in ys:
                for x0 in xs:
                    crop = rgb.crop((x0, y0, x0 + size, y0 + size))
                    probability[y0 : y0 + size, x0 : x0 + size] += self._infer(crop)
                    counts[y0 : y0 + size, x0 : x0 + size] += 1.0
            probability /= np.maximum(counts, 1.0)

        mask = probability >= self.threshold
        if self.close_iterations > 0:
            mask = binary_close(mask, self.close_iterations)
        return mask


def _static_input_size(shape, fallback: int, layout: str) -> int:
    if len(shape) == 4:
        height, width = (shape[2], shape[3]) if layout == "nchw" else (shape[1], shape[2])
        if isinstance(height, int) and isinstance(width, int):
            return max(height, width)
    return fallback


class HeuristicBuildingSegmenter:
    """Offline fallback: non-vegetated, low-luminance, locally-contrasted roof pixels."""

    def __init__(self, luminance_max: float = 128.0, close_iterations: int = 2):
        self.luminance_max = luminance_max
        self.close_iterations = close_iterations
        self.stats = SegmenterStats(
            backend="heuristic",
            model_path="",
            input_size=0,
            threshold=0.0,
            stride=0,
            normalization="none",
            output_mode="binary",
        )

    def segment(self, image: Image.Image) -> np.ndarray:
        array = np.asarray(image.convert("RGB"), dtype=np.float32)
        red, green, blue = array[..., 0], array[..., 1], array[..., 2]
        total = red + green + blue + 1.0
        exg = (2.0 * green - red - blue) / total
        luminance = 0.299 * red + 0.587 * green + 0.114 * blue
        mask = (exg < 0.04) & (luminance < self.luminance_max) & (luminance > 12.0)
        mask = binary_close(mask, self.close_iterations)
        return binary_open(mask, 1)


def polygons_to_local(
    polygons: list[Polygon],
    pixel_to_local,
    extent: int = 4096,
    clip_buffer: int = 64,
) -> list[Polygon]:
    """Transforms pixel-space polygons into tile-local space and clips to the tile buffer."""
    clip = box(-clip_buffer, -clip_buffer, extent + clip_buffer, extent + clip_buffer)
    result: list[Polygon] = []
    for polygon in polygons:
        local = transform(pixel_to_local, polygon)
        if local.is_empty:
            continue
        if not local.is_valid:
            local = make_valid(local)
        clipped = local.intersection(clip)
        for part in iter_polygons(clipped):
            if part.area > 0:
                result.append(part)
    return result


def sample_grid(geometry, step: float = 2.0):
    min_x, min_y, max_x, max_y = geometry.bounds
    xs = np.arange(math.floor(min_x), math.ceil(max_x) + 1, step, dtype=np.float64)
    ys = np.arange(math.floor(min_y), math.ceil(max_y) + 1, step, dtype=np.float64)
    if xs.size == 0 or ys.size == 0:
        return np.empty((0, 2), dtype=np.float64)
    grid_x, grid_y = np.meshgrid(xs, ys)
    return np.stack((grid_x.ravel(), grid_y.ravel()), axis=1)

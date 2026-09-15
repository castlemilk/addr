"""SAM 3 text-promptable ONNX segmentation used to refine roof outlines.

Runs the split vision/text/decoder ONNX export from
`danilobukvic/sam3-text-onnx`. The vision encoder is the expensive part, so
image features are computed once per tile and reused across prompts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

LOGGER = logging.getLogger("ml.sam3")

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

VISION_FILES = ["vision_encoder_int8.onnx", "vision_encoder.onnx"]
TEXT_FILES = ["text_encoder_int8.onnx", "text_encoder.onnx"]
DECODER_FILES = ["decoder_int8.onnx", "decoder.onnx"]
MAX_TOKENS = 32


@dataclass
class Sam3Proposal:
    mask: np.ndarray
    score: float
    box: tuple[float, float, float, float]


class Sam3TextSegmenter:
    """Open-vocabulary segmentation from text prompts (e.g. "house", "roof", "trees")."""

    def __init__(
        self,
        models_dir: str | Path,
        input_size: int = 1008,
        providers: list[str] | None = None,
    ):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.models_dir = Path(models_dir)
        self.input_size = input_size

        vision_path = self._resolve(VISION_FILES)
        text_path = self._resolve(TEXT_FILES)
        decoder_path = self._resolve(DECODER_FILES)
        tokenizer_path = self.models_dir / "tokenizer.json"
        if not tokenizer_path.exists():
            raise FileNotFoundError(f"missing SAM3 tokenizer at {tokenizer_path}")

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        active_providers = providers or ["CPUExecutionProvider"]
        self.vision = ort.InferenceSession(
            str(vision_path), sess_options=options, providers=active_providers
        )
        self.text = ort.InferenceSession(
            str(text_path), sess_options=options, providers=active_providers
        )
        self.decoder = ort.InferenceSession(
            str(decoder_path), sess_options=options, providers=active_providers
        )
        self.decoder_inputs = {spec.name for spec in self.decoder.get_inputs()}

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding(length=MAX_TOKENS, pad_id=0)
        LOGGER.info(
            "loaded SAM3 ONNX sessions",
            extra={"dir": str(self.models_dir), "providers": active_providers},
        )

    def _resolve(self, candidates: list[str]) -> Path:
        for name in candidates:
            path = self.models_dir / name
            if path.exists():
                return path
        raise FileNotFoundError(
            f"none of {candidates} found in {self.models_dir}; "
            "run `python -m scripts.ml.fetch_models --presets sam3`"
        )

    def encode_image(self, image: Image.Image) -> list[np.ndarray]:
        resized = image.convert("RGB").resize(
            (self.input_size, self.input_size), Image.Resampling.BILINEAR
        )
        array = np.asarray(resized, dtype=np.float32) / 255.0
        array = (array - IMAGENET_MEAN) / IMAGENET_STD
        tensor = np.transpose(array, (2, 0, 1))[None]
        return self.vision.run(None, {"pixel_values": tensor})

    def _tokenize(self, prompt: str) -> tuple[np.ndarray, np.ndarray]:
        encoded = self.tokenizer.encode(prompt)
        return (
            np.asarray([encoded.ids], dtype=np.int64),
            np.asarray([encoded.attention_mask], dtype=np.int64),
        )

    def proposals(
        self,
        features: list[np.ndarray],
        prompt: str,
        image_size: tuple[int, int],
        threshold: float = 0.4,
        max_proposals: int = 128,
    ) -> list[Sam3Proposal]:
        input_ids, attention_mask = self._tokenize(prompt)
        text_features = self.text.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )[0]
        feed = {
            "fpn_hidden_state_0": features[0],
            "fpn_hidden_state_1": features[1],
            "fpn_hidden_state_2": features[2],
            "fpn_position_encoding_0": features[4],
            "fpn_position_encoding_1": features[5],
            "fpn_position_encoding_2": features[6],
            "text_features": text_features,
            "attention_mask": attention_mask,
        }
        outputs = self.decoder.run(
            None, {key: value for key, value in feed.items() if key in self.decoder_inputs}
        )
        pred_masks, pred_boxes, pred_logits = outputs
        scores = 1.0 / (1.0 + np.exp(-np.asarray(pred_logits)[0]))

        order = np.argsort(-scores)
        proposals: list[Sam3Proposal] = []
        for index in order:
            if scores[index] < threshold or len(proposals) >= max_proposals:
                break
            mask_logits = np.asarray(pred_masks[0, index], dtype=np.float32)
            mask = np.asarray(
                Image.fromarray(mask_logits, mode="F").resize(
                    image_size, Image.Resampling.BILINEAR
                ),
                dtype=np.float32,
            )
            box_values = [float(value) for value in pred_boxes[0, index]]
            proposals.append(
                Sam3Proposal(
                    mask=mask > 0.0,
                    score=float(scores[index]),
                    box=(
                        box_values[0],
                        box_values[1],
                        box_values[2],
                        box_values[3],
                    ),
                )
            )
        return proposals

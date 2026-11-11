"""Corner-preserving roof outline model: torchvision Mask R-CNN (ResNet-50 FPN v2) at native resolution.

Why these changes to the stock model (see README for the full rationale):
  * SAM3's masks come out at 288 x 288 for a 1152 px, 0.236 m/px tile, i.e. one mask cell per metre, then bilinear
    upsampling rounds every corner. The stock Mask R-CNN head predicts 28 x 28 per box; here RoIAlign pools 28 x 28
    and the deconvolution doubles it to 56 x 56, so a 20 m house gets a 0.36 m mask cell and a 6 m shed 0.11 m.
  * The image is never resized (NativeResolutionTransform): the model sees the saved PNG's pixels 1:1.
  * The mask loss up-weights pixels on the target boundary (where corners live) and multiplies each instance by its
    provenance weight (human outlines 8-10x, pseudo-labels 1x).
  * Anchors start at 16 px (3.8 m) so garden sheds get proposals.

Imports torch; nothing in the main ML venv imports this module.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import torch
import torch.nn.functional as F
from torch import nn
from torchvision.models import ResNet50_Weights
from torchvision.models.detection import MaskRCNN_ResNet50_FPN_V2_Weights, maskrcnn_resnet50_fpn_v2
from torchvision.models.detection import roi_heads as tv_roi_heads
from torchvision.models.detection.anchor_utils import AnchorGenerator
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
from torchvision.models.detection.mask_rcnn import MaskRCNNPredictor
from torchvision.models.detection.transform import GeneralizedRCNNTransform
from torchvision.ops import MultiScaleRoIAlign

MODEL_VERSION = 'roof-outline-maskrcnn-r50fpnv2-m56-v1'
DEFAULTS = {'pretrained': 'coco', 'mask_resolution': 56, 'anchor_sizes': [16, 32, 64, 128, 256],
            'detections_per_img': 200, 'trainable_backbone_layers': 5, 'boundary_weight': 3.,
            'box_score_thresh': .05, 'rpn_post_nms_top_n_test': 1000}


class NativeResolutionTransform(GeneralizedRCNNTransform):
    """Normalise and pad only; never rescale (pixel size is a property of the imagery, not a hyper-parameter)."""

    def resize(self, image, target=None):
        return image, target


# --- weighted, boundary-aware mask loss ----------------------------------------------------------------

_LOSS = threading.local()


def set_instance_weights(weights, boundary_weight):
    """Per-image instance weight tensors for the next forward pass (read by the patched mask loss)."""
    _LOSS.weights = weights
    _LOSS.boundary_weight = float(boundary_weight)


def clear_instance_weights():
    _LOSS.weights = None


def boundary_band(targets, width=1):
    """1 where a (M x M) target mask changes within `width` cells: the outline and its corners."""
    binary = (targets >= .5).float()[:, None]
    kernel = 2 * width + 1
    dilated = F.max_pool2d(binary, kernel, stride=1, padding=width)
    eroded = -F.max_pool2d(-binary, kernel, stride=1, padding=width)
    return (dilated - eroded)[:, 0]


def weighted_maskrcnn_loss(mask_logits, proposals, gt_masks, gt_labels, mask_matched_idxs):
    """torchvision's maskrcnn_loss plus per-pixel boundary weights and per-instance provenance weights."""
    size = mask_logits.shape[-1]
    labels = torch.cat([gt_label[idxs] for gt_label, idxs in zip(gt_labels, mask_matched_idxs, strict=True)], dim=0)
    targets = torch.cat([tv_roi_heads.project_masks_on_boxes(m, p, i, size)
                         for m, p, i in zip(gt_masks, proposals, mask_matched_idxs, strict=True)], dim=0)
    if targets.numel() == 0:
        return mask_logits.sum() * 0
    logits = mask_logits[torch.arange(labels.shape[0], device=labels.device), labels].float()
    targets = targets.float()
    boundary_weight = getattr(_LOSS, 'boundary_weight', 1.)
    pixel_weights = 1. + (boundary_weight - 1.) * boundary_band(targets)
    per_instance = F.binary_cross_entropy_with_logits(logits, targets, weight=pixel_weights, reduction='none')
    per_instance = per_instance.sum((1, 2)) / pixel_weights.sum((1, 2))
    weights = getattr(_LOSS, 'weights', None)
    if weights is None:
        return per_instance.mean()
    instance_weights = torch.cat([w.to(per_instance.device)[idxs] for w, idxs in zip(weights, mask_matched_idxs, strict=True)])
    return (per_instance * instance_weights).sum() / instance_weights.sum().clamp_min(1e-6)


def install_weighted_mask_loss():
    """Swap torchvision's module-level mask loss for the weighted one (idempotent, training processes only)."""
    tv_roi_heads.maskrcnn_loss = weighted_maskrcnn_loss


# --- model ---------------------------------------------------------------------------------------------

def model_config(overrides=None):
    config = dict(DEFAULTS)
    config.update({k: v for k, v in (overrides or {}).items() if k in DEFAULTS and v is not None})
    return config


def build_model(config=None):
    """Two-class (background, roof) Mask R-CNN with a 56 x 56 mask head and native-resolution input."""
    config = model_config(config)
    pretrained = config['pretrained']
    if pretrained not in ('coco', 'imagenet', 'none'):
        raise ValueError(f'pretrained must be coco, imagenet or none, not {pretrained!r}')
    model = maskrcnn_resnet50_fpn_v2(
        weights=MaskRCNN_ResNet50_FPN_V2_Weights.COCO_V1 if pretrained == 'coco' else None,
        weights_backbone=ResNet50_Weights.IMAGENET1K_V1 if pretrained == 'imagenet' else None,
        trainable_backbone_layers=config['trainable_backbone_layers'] if pretrained != 'none' else None,
        rpn_post_nms_top_n_test=config['rpn_post_nms_top_n_test'],
        box_detections_per_img=config['detections_per_img'], box_score_thresh=config['box_score_thresh'])
    heads = model.roi_heads
    heads.box_predictor = FastRCNNPredictor(heads.box_predictor.cls_score.in_features, 2)
    old = heads.mask_predictor
    heads.mask_roi_pool = MultiScaleRoIAlign(['0', '1', '2', '3'], output_size=config['mask_resolution'] // 2, sampling_ratio=2)
    predictor = MaskRCNNPredictor(256, 256, 2)
    predictor.conv5_mask.load_state_dict(old.conv5_mask.state_dict())   # keep COCO's upsampling filters
    heads.mask_predictor = predictor
    sizes = tuple((int(s),) for s in config['anchor_sizes'])
    model.rpn.anchor_generator = AnchorGenerator(sizes, ((.5, 1., 2.),) * len(sizes))
    old_transform = model.transform
    model.transform = NativeResolutionTransform(min_size=800, max_size=1333, image_mean=old_transform.image_mean,
                                                image_std=old_transform.image_std)
    model.addr_config = config
    return model


def load_checkpoint_model(path, map_location='cpu'):
    """(model in eval mode, checkpoint dict) from a train.py checkpoint; never downloads weights."""
    checkpoint = torch.load(path, map_location=map_location, weights_only=False)
    config = dict(checkpoint['model_config'])
    config['pretrained'] = 'none'
    model = build_model(config)
    model.load_state_dict(checkpoint['model'])
    model.eval()
    return model, checkpoint


class ExportWrapper(nn.Module):
    """One image [3, H, W] in [0, 1] -> boxes [N, 4] (pixels), scores [N], mask_probs [N, 1, M, M] (unpasted).

    Pasting M x M masks into the frame happens in numpy (infer.paste_masks), so the ONNX graph never materialises
    N full-resolution float masks (80 roofs x 1152^2 x 4 bytes would be 425 MB per tile).
    """

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, image):
        images, _ = self.model.transform([image])
        features = self.model.backbone(images.tensors)
        if isinstance(features, torch.Tensor):
            features = OrderedDict([('0', features)])
        proposals, _ = self.model.rpn(images, features)
        detections, _ = self.model.roi_heads(features, proposals, images.image_sizes)
        result = detections[0]
        return result['boxes'], result['scores'], result['masks']


@torch.no_grad()
def predict(model, image, device):
    """torch inference for one float image tensor [3, H, W]: (boxes np, scores np, mask_probs np [N, M, M])."""
    boxes, scores, masks = ExportWrapper(model).to(device).eval()(image.to(device))
    return boxes.float().cpu().numpy(), scores.float().cpu().numpy(), masks[:, 0].float().cpu().numpy()

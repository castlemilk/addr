"""Static-image, differentiable EdgeTAM adapter. No CUDA postprocessing/video memory."""
from __future__ import annotations

from pathlib import Path
import torch
from torch import nn
import torch.nn.functional as F

EDGETAM_REV = '7711e012a30a2402c4eaab637bdb00a521302c91'


class TinyControl(nn.Module):
    """Plumbing control only: not RoofTAM, not an accuracy baseline."""
    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(nn.Conv2d(3, 8, 3, padding=1), nn.ReLU(), nn.Conv2d(8, 1, 1))

    def forward(self, images, boxes):
        return self.net(images)


class EdgeRoof(nn.Module):
    def __init__(self, checkpoint: str):
        super().__init__()
        if not checkpoint or not Path(checkpoint).is_file():
            raise ValueError('EdgeTAM requires the pinned pretrained checkpoint; no random-weight fallback')
        from sam2.build_sam import build_sam2
        self.model = build_sam2('configs/edgetam.yaml', checkpoint, device='cpu',
                               apply_postprocessing=False)
        self.model.requires_grad_(False)
        self.model.sam_mask_decoder.requires_grad_(True)
        self.register_buffer('mean', torch.tensor([.485, .456, .406]).view(1, 3, 1, 1))
        self.register_buffer('std', torch.tensor([.229, .224, .225]).view(1, 3, 1, 1))
        self.train(False)

    def train(self, mode: bool = True):
        super().train(mode)
        # Frozen encoder BatchNorm statistics must not drift during adaptation.
        self.model.eval()
        self.model.sam_mask_decoder.train(mode)
        return self

    def forward(self, images, boxes):
        if images.shape[-2:] != (1024, 1024):
            raise ValueError('Pinned EdgeTAM uses fixed 1024x1024 letterboxed inputs')
        # Do NOT use SAM2ImagePredictor: its no_grad methods would break training.
        # forward_image includes trainable high-resolution decoder projections.
        encoded = self.model.forward_image((images-self.mean)/self.std)
        features = encoded['backbone_fpn'][-self.model.num_feature_levels:]
        embedding = features[-1]
        if self.model.directly_add_no_mem_embed:
            embedding = embedding + self.model.no_mem_embed.reshape(1, -1, 1, 1)
        sparse, dense = self.model.sam_prompt_encoder(points=None, boxes=boxes, masks=None)
        logits, _, _, _ = self.model.sam_mask_decoder(
            image_embeddings=embedding,
            image_pe=self.model.sam_prompt_encoder.get_dense_pe(),
            sparse_prompt_embeddings=sparse, dense_prompt_embeddings=dense,
            multimask_output=False, repeat_image=False, high_res_features=features[:-1])
        return logits


def segmentation_loss(logits, target, valid):
    target = F.interpolate(target.float(), logits.shape[-2:], mode='nearest')
    valid = F.interpolate(valid.float(), logits.shape[-2:], mode='nearest')
    # Target-derived boundary weights are for the TRAIN loss, never inference prompts.
    band = F.max_pool2d(target, 3, 1, 1) + F.max_pool2d(-target, 3, 1, 1)
    weight = (1 + 2*band) * valid
    bce = (F.binary_cross_entropy_with_logits(logits.float(), target, reduction='none') * weight).sum()
    bce = bce / weight.sum().clamp_min(1)
    p = logits.float().sigmoid() * valid
    truth = target * valid
    dice = 1 - (2*(p*truth).sum((1,2,3)) + 1) / (p.sum((1,2,3)) + truth.sum((1,2,3)) + 1)
    return bce + dice.mean()


def trainable_state(model):
    return {n: p.detach().cpu().clone() for n,p in model.named_parameters() if p.requires_grad}


def load_trainable(model, state):
    params = {n:p for n,p in model.named_parameters() if p.requires_grad}
    if params.keys() != state.keys():
        raise ValueError('Adapter parameters do not match this model')
    with torch.no_grad():
        for name, parameter in params.items():
            parameter.copy_(state[name].to(parameter.device))

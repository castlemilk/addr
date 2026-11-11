# Roof outline training image for a rented Linux GPU box (NVIDIA driver >= 525).
# Build context: the code bundle directory that run.sh unpacks on the box (scripts/ only; data is mounted).
#   docker build -t addr-roof-train -f scripts/ml/training/deploy/Dockerfile .
# The torch cu126 wheels bring their own CUDA/cuDNN libraries; the CUDA base supplies the driver-facing runtime.
FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TORCH_HOME=/opt/torch \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && python3.12 -m venv /opt/venv

COPY scripts/ml/training/requirements-gpu.txt /tmp/requirements-gpu.txt
RUN pip install -r /tmp/requirements-gpu.txt

# Bake the COCO Mask R-CNN v2 weights (~180 MB) into the image so the timed run never waits on a download.
RUN python -c "from torchvision.models.detection import MaskRCNN_ResNet50_FPN_V2_Weights as W; W.COCO_V1.get_state_dict(progress=False)"

WORKDIR /app
COPY scripts/__init__.py scripts/__init__.py
COPY scripts/ml/__init__.py scripts/ml/sam3.py scripts/ml/segmentation.py scripts/ml/tiles.py scripts/ml/
COPY scripts/ml/training scripts/ml/training

# tini forwards SIGTERM from `timeout`/`docker stop` so train.py saves last.pt before exiting.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "scripts.ml.training.train", "--help"]

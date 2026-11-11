#!/bin/sh
cd /Users/benebsworth/projects/addr
LOG=data/reviews/eyes/training/sam3-mac/sweep/stage1.log
until grep -q "sweep stage1 done" "$LOG" 2>/dev/null; do sleep 60; done
until [ -f data/reviews/eyes/training/sam3-mac/coco-teachers-squared-v1/manifest.json ]; do sleep 60; done
LR=$(scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.sweep_next)
echo "$(date -u +%FT%TZ) stage1 done, best lr = $LR" >> data/reviews/eyes/training/sam3-mac/sweep/stage2.log
if [ "$LR" = "none" ]; then echo "no qualifying run; stage 2 not started" >> data/reviews/eyes/training/sam3-mac/sweep/stage2.log; exit 0; fi
sh scripts/ml/training/sam3_mac/sweep.sh stage2 "$LR" >> data/reviews/eyes/training/sam3-mac/sweep/stage2.log 2>&1

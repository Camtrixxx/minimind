#!/usr/bin/env bash
set -euo pipefail

ROOT="/data/heyuhang/minimind"
LOG_DIR="$ROOT/logs"
WATCH_LOG="$LOG_DIR/watch_pretrain_then_sft_1024x12.log"
SFT_LOG="$LOG_DIR/full_sft_hyh_full_1024x12.log"

PRETRAIN_PATTERN="train_pretrain.py --save_weight pretrain_hyh_full_1024x12"
SFT_PATTERN="train_full_sft.py --save_weight full_sft_hyh_full_1024x12"
PRETRAIN_WEIGHT="$ROOT/out/pretrain_hyh_full_1024x12_1024.pth"

mkdir -p "$LOG_DIR"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$WATCH_LOG"
}

log "Watcher started."

while pgrep -af "$SFT_PATTERN" >/dev/null; do
  log "SFT is already running; watcher exits."
  exit 0
done

while pgrep -af "$PRETRAIN_PATTERN" >/dev/null; do
  log "Pretrain still running; waiting 300s."
  sleep 300
done

log "Pretrain process is no longer running."

if [[ ! -s "$PRETRAIN_WEIGHT" ]]; then
  log "ERROR: expected pretrain weight not found or empty: $PRETRAIN_WEIGHT"
  exit 1
fi

if pgrep -af "$SFT_PATTERN" >/dev/null; then
  log "SFT was started by another process; watcher exits."
  exit 0
fi

log "Starting SFT. Logs: $SFT_LOG"
cd "$ROOT/trainer"
conda run -n hyh-llm torchrun --nproc_per_node 8 train_full_sft.py \
  --save_weight full_sft_hyh_full_1024x12 \
  --from_weight pretrain_hyh_full_1024x12 \
  --hidden_size 1024 \
  --num_hidden_layers 12 \
  --epochs 2 \
  --batch_size 8 \
  --accumulation_steps 2 \
  --learning_rate 8e-6 \
  --max_seq_len 768 \
  --data_path ../dataset/sft_t2t.jsonl \
  --save_interval 1000 \
  --log_interval 100 \
  >> "$SFT_LOG" 2>&1

log "SFT command finished with exit code $?."

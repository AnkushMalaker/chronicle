#!/bin/bash
# Poll the A100 training log via the Jupyter contents API (SSH-free) until the
# early-stopping loop reports it hit <2% WER ("TARGET REACHED" / "DONE"), or the
# training process dies, or a long timeout. Exits 0 with a summary so the agent is
# re-invoked to retrieve the adapter + pause the instance.
set -u
ENVBKP=/home/ankush/workspaces/friend-lite/.env.bkp
FT=/home/ankush/workspaces/friend-lite/extras/asr-services/providers/gemma4/finetune
JTOKEN=$(grep -hiE "^jarvislabs=" "$ENVBKP" | head -1 | cut -d= -f2- | tr -d '"'\'' \r')

# Resolve host + jupyter token once.
URL=$(JLTOKEN="$JTOKEN" uv run --with "git+https://github.com/jarvislabsai/jlclient.git" python3 -c "
import os; from jlclient import jarvisclient; from jlclient.jarvisclient import *
jarvisclient.token=os.environ['JLTOKEN']; print(User.get_instances()[0].url)" 2>/dev/null | grep notebooks)
HOST=$(echo "$URL" | sed -E 's#https://([^/]+)/.*#\1#')
TOK=$(echo "$URL" | sed -E 's#.*token=##')
LOG="https://$HOST/api/contents/gemma4ft/out/train_full2.log?token=$TOK&type=file&format=text&content=1"
echo "polling host=$HOST"

for i in $(seq 1 240); do   # up to 240 * 4min = 16h
  C=$(curl -s --max-time 90 "$LOG" 2>/dev/null)
  # latest epoch-mean signal: last WER eval line and last loss/epoch
  EVAL=$(echo "$C" | grep -aoE '\[epoch [0-9]+\][^\\]*(corpus WER = [0-9.]+%|skipping WER eval)' | tail -1)
  TGT=$(echo "$C" | grep -aoE 'TARGET REACHED[^\\]*|DONE \(final adapter saved\)')
  LOSSEP=$(echo "$C" | grep -aoE "'loss': '[0-9.]+', 'grad_norm'[^}]*'epoch': '[0-9.]+'" | tail -1 | sed -E "s/, 'grad_norm'[^,]*,/,/")
  echo "[$(date +%H:%M)] iter=$i | $LOSSEP | eval: ${EVAL:-none}"
  if [ -n "$TGT" ]; then echo "STOP_SIGNAL: $TGT"; exit 0; fi
  # detect dead training: if process gone (no recent log growth is hard; rely on TARGET/DONE or manual)
  sleep 240
done
echo "poller timed out after 16h without TARGET"
exit 0

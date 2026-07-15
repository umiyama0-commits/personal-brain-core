#!/bin/bash
# Plaud 議事録の日次取り込み (Drive 社長室/Plaud → Brain wiki/meetings/)
# crontab エントリ:
#   0 8 * * * /Users/brain/brain-agent/scripts/plaud_daily_sync.sh >> /Users/brain/brain-agent/data/brain/scrape.log 2>&1

cd /Users/brain/brain-agent

# .env を source (cron は親 shell の env を継承しないので必須)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

echo "$(date): ===== Plaud daily sync start ====="

# Drive 「社長室 / Plaud」フォルダから新規 transcript を取得
# folder_id は .gdrive_sources.json の plaud-exports source と同じ
python3 gdrive_sync.py \
    --label plaud-exports \
    --folder 10SED54K073DmedpkhPRVE2azrTzXXrfo

echo "$(date): Plaud daily sync complete."
echo "$(date): ===== Plaud daily sync done ====="

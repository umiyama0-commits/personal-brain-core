#!/bin/bash
# start_tunnel.sh — Cloudflare Tunnel起動 + LINE Webhook自動更新
# LaunchAgent経由でMac起動時に自動実行

LOG="/Users/brain/brain-agent/data/brain/tunnel.log"
ENV_FILE="/Users/brain/brain-agent/.env"

source "$ENV_FILE"

# 既存のcloudflaredを停止
pkill -f "cloudflared tunnel --url" 2>/dev/null
sleep 2

# トンネル起動（バックグラウンド）
/opt/homebrew/bin/cloudflared tunnel --url http://localhost:8000 --protocol quic > /tmp/cloudflared_output.log 2>&1 &
TUNNEL_PID=$!

# URLが出るまで待機
for i in $(seq 1 30); do
    TUNNEL_URL=$(grep -o 'https://[a-z\-]*\.trycloudflare\.com' /tmp/cloudflared_output.log 2>/dev/null | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
    echo "$(date) ERROR: Tunnel URL not found" >> "$LOG"
    exit 1
fi

echo "$(date) Tunnel: $TUNNEL_URL (PID: $TUNNEL_PID)" >> "$LOG"

# LINE Webhook更新
curl -s -X PUT https://api.line.me/v2/bot/channel/webhook/endpoint \
    -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"endpoint\": \"${TUNNEL_URL}/webhook\"}" > /dev/null

echo "$(date) LINE Webhook updated: ${TUNNEL_URL}/webhook" >> "$LOG"

# URLをファイルに保存（参照用）
echo "$TUNNEL_URL" > /Users/brain/brain-agent/data/brain/tunnel_url.txt

# トンネルが死んだら再起動するループ
while true; do
    if ! kill -0 $TUNNEL_PID 2>/dev/null; then
        echo "$(date) Tunnel died, restarting..." >> "$LOG"
        exec "$0"
    fi
    sleep 60
done

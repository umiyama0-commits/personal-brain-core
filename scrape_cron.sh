#!/bin/bash
# 全チャネル スクレイピング定期実行（9:00-24:00 / 2時間おき）
# crontab -e で以下を追加:
#   0 9,11,13,15,17,19,21,23 * * * /Users/brain/brain-agent/scrape_cron.sh >> /Users/brain/brain-agent/data/brain/scrape.log 2>&1

cd /Users/brain/brain-agent

# .env を source して OWNDAYS_MOBILE_USER 等を環境変数に展開
# (cron は親 shell の env を継承しないので必須)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

echo "$(date): ===== Scrape start ====="

# Google Calendar + Gmail (API)
echo "$(date): Starting Google sync..."
python3 google_sync.py --days 1 --max-emails 30
echo "$(date): Google sync complete."

# LINE Works
# ★2026-06-08 システム評価 0-1: 平文 hardcode を除去し env 経由に (§1.1)。
# 変数名は .env.example の既存規約 LINEWORKS_USER / LINEWORKS_PASS。設定すると
# lineworks_scraper.py が argparse default で読む。未設定なら loud-skip (silent break 回避)。
# ※ 旧 password は git 履歴に残存 → 海山がローテーション要。
if [ -n "$LINEWORKS_USER" ] && [ -n "$LINEWORKS_PASS" ]; then
    echo "$(date): Starting LINE Works scrape..."
    python3 lineworks_scraper.py --rooms 20
    echo "$(date): LINE Works complete."
else
    echo "$(date): WARNING: LINEWORKS_USER/PASS 未設定 → LINE Works scrape を skip (.env に設定要)"
fi

# Claude.ai
echo "$(date): Starting Claude.ai scrape..."
python3 claude_scraper.py --count 20
echo "$(date): Claude.ai complete."

# ChatGPT
echo "$(date): Starting ChatGPT scrape..."
python3 chatgpt_scraper.py --count 20
echo "$(date): ChatGPT complete."

# OWNDAYS Net Mobile 売上データ (2026-04-24: ログイン成功。本番稼働)
# 認証は env (OWNDAYS_MOBILE_USER / OWNDAYS_MOBILE_PASS) 経由、Cookie は data/brain/.mobile_owndays_cookies.json に自動保存。
# 6 セクション取得 → post-process で wiki/knowledge/owndays-daily-{sales,stores}.md 生成。
# うみやまAI は daily-stores.md を core に含めるので、全店舗今日の売上を即答可能。
if [ -n "$OWNDAYS_MOBILE_USER" ] && [ -n "$OWNDAYS_MOBILE_PASS" ]; then
    echo "$(date): Starting OWNDAYS Net Mobile scrape..."
    python3 mobile_owndays_scraper.py --fetch
    echo "$(date): OWNDAYS Net Mobile complete."
fi

# OWNDAYS 過去履歴 incremental (毎晩 23:xx に実行 — その日の確定後)
#  ★2026-04-28: 09:00 → 23:00 に移動。理由は以下:
#    /api/totaldaily が朝早く叩くと「前日の確定前 or リセット直後」の値を返すバグ観測。
#    例: 4/27 (月) Web スクレイプ 23:04 = 88M / 5,000客 (正常)
#         4/28 09:05 API call → 4/27 = 0.9M / 90客 (誤り、100倍ズレ)
#    23:00 は同 cron の OWNDAYS daily-sales scrape 直後 (= 当日の数字が確定済み) なので
#    API も正しい値を返す可能性が高い。
#  - totaldaily: cache の最終日+1 〜 today の差分のみ
#  - monthly / monthly_stores: 当月 + 前月 のみ refetch (履歴は変わらない)
#  - 数十秒〜数分で完了
if [ -n "$OWNDAYS_MOBILE_USER" ] && [ -n "$OWNDAYS_MOBILE_PASS" ] && [ "$(date +%H)" = "23" ]; then
    echo "$(date): Starting OWNDAYS historical incremental fetch..."
    python3 mobile_owndays_historical.py --incremental
    echo "$(date): OWNDAYS historical incremental complete."
fi

# ★2026-05-07 追加 / ★2026-05-18 毎サイクル化: 国別/エリア別/業態別/リーグ別 日次履歴 wiki
# 「昨日の日本の売上」「昨日の関東Aの売上」など query が直接答えられる仕組み。
#
# ★2026-05-18 修正 (海外2日前 11-13% ズレ事故): 以前は 23:00 のみ実行だったが、
# 海外 (シンガポール/台湾/タイ) は時差で JST 23:00 時点でまだ売上が確定しておらず、
# その未確定値で wiki が固定 → 翌 23:00 まで stale のまま「昨日の海外売上」が誤答。
# build_store_daily_history.py と同様に毎サイクル (2h おき 9-23時) に再構築する。
# raw txt を読むだけなので追加コスト < 1 秒。翌朝のサイクルで前日値が確定値に自動補正。
echo "$(date): Starting breakdown history rebuild..."
python3 scripts/build_breakdown_history.py
echo "$(date): Breakdown history rebuild complete."

# ★2026-05-14 追加: 店舗 × 日次履歴 wiki を構築
# scraper が import/processed/owndays_mobile_sales_storelist_*.txt に毎日上書き保存している
# raw を読み、店舗ごとに過去 90 日の日次データを集計 → owndays-history-storesdaily.md。
# 「吉祥寺の最近の推移」「ららぽーと湘南平塚の過去 1 週間」型クエリに直接答えられる仕組み。
# どの時刻でも実行可能 (= 全 cron サイクルで最新化、追加コスト < 1 秒)。
echo "$(date): Starting store daily history rebuild..."
python3 scripts/build_store_daily_history.py
echo "$(date): Store daily history rebuild complete."

# ★2026-05-19 追加: 都道府県/AM/SV 別 月次・日次 wiki 再集計。
# store_master を権威ソース (owndays-area-managers.md) から再生成 →
# build_grouped_monthly が prefecture/am/sv × month/day wiki を生成。
# build_store_daily_history の後 (storesdaily.md が最新化された後) に走らせる。
echo "$(date): Starting store master + grouped monthly/daily rebuild..."
python3 scripts/build_store_master.py
python3 scripts/build_grouped_monthly.py
echo "$(date): Grouped (prefecture/AM/SV) rebuild complete."

# OWNDAYS 過去履歴 フルリフレッシュ — 日曜 23:xx のみ (整合性チェック用)
# ★2026-04-28: 同様に 09:00 → 23:00 に移動。
# incremental が壊れた時の保険。重い (数分〜10分)
if [ -n "$OWNDAYS_MOBILE_USER" ] && [ -n "$OWNDAYS_MOBILE_PASS" ] && [ "$(date +%u)" = "7" ] && [ "$(date +%H)" = "23" ]; then
    echo "$(date): Starting OWNDAYS historical FULL refresh (Sunday safety net)..."
    python3 mobile_owndays_historical.py --years 3 --all
    echo "$(date): OWNDAYS historical full complete."
fi

# Google Drive 取り込み — 月曜 21:xx のみ (週次)
#  ★2026-05-19: 日曜 11:00 → 月曜 21:00 に移動。理由:
#    Monday Dash (週次/月次レポート) は月曜に確定・公開されるため、
#    日曜取得だと「先週分の古いレポート」しか拾えず最新週次を取り逃す。
#    月曜夜 (21:00、レポート確定後) に取得することで当週分を確実に拾う。
#    23:00 は OWNDAYS historical/breakdown/store_master 等で重いので 21:00。
# .gdrive_sources.json に列挙されたフォルダ群を selective に取り込み
# (人事評価/給与/機密系は DEFAULT_EXCLUDE_PATTERN で常時ブロック)
if [ "$(date +%u)" = "1" ] && [ "$(date +%H)" = "21" ]; then
    echo "$(date): Starting Google Drive selective sync (weekly)..."
    python3 gdrive_sync.py --all
    echo "$(date): Google Drive sync complete."
fi

echo "$(date): ===== Scrape done ====="

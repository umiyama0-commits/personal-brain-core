#!/usr/bin/env bash
# scripts/whitespace_refresh.sh — 眼鏡チェーン空白地ダッシュボードの月次 店舗+施設データ更新
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-07-01 海山指示「月一回 OWNDAYS と主要競合の出店状況をアップデート」。
# 範囲 = 店舗(公式API/scraper)+ モール(OSM)のみ再取得 → matrix 再計算 → HTML 再生成 → deploy。
# 境界/人口/所得は committed base(output_*/raw/gov 等、git 追跡)を固定使用=月次では触らない。
# 対象: SG / TH / TW / JP。cron: 毎月1日 04:10(cron_install.sh)。JP は ~30-45分(全チェーン再scrape+geocode)。
#
# 設計:
#  - 各国は独立ガード(1国失敗で他を止めない)。
#  - sanity gate: 店舗数 / モール数の下限を検証(Overpass 空→CSV header-only の silent 劣化を検知)。
#    gate 通過時のみ HTML を data/brain/web/ へ cp(失敗時は前回の HTML を維持=keep-last-good)。
#  - §1.18 loud_fail: 国別に成否を記録(3回連続失敗 + cooldown で LINE 通知)。
#  - 生成 HTML に差分があれば git commit + push(rebase retry で auto_deploy と競合回避)→ auto_deploy が反映。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
# shellcheck disable=SC1091
source "$ROOT/scripts/cron_env.sh"

PY="$ROOT/whitespace_analysis/.venv/bin/python3"
[ -x "$PY" ] || PY="python3"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [whitespace_refresh]" "$@"; }
log "start"

# --- deps 確保 (system python + user site。無ければ導入、それでも不可なら loud abort) ---
if ! "$PY" -c "import curl_cffi, shapely, pandas" 2>/dev/null; then
    log "deps 不足 → pip install --user"
    "$PY" -m pip install --user -q curl_cffi shapely pandas pyshp 2>&1 | tail -2 || true
    if ! "$PY" -c "import curl_cffi, shapely, pandas" 2>/dev/null; then
        "$PY" -c "import sys; sys.path.insert(0,'$ROOT'); from clone_improve_lib import loud_fail; loud_fail('whitespace_refresh', False, 'python deps (curl_cffi/shapely/pandas) 導入不可 — 全国 skip', threshold=1, cooldown_h=24)" 2>/dev/null || true
        log "FATAL: deps 不可、abort"; exit 1
    fi
fi

# loud_fail ラッパ (§1.18、cron_env.sh source 前提)
_loud() { # _loud <component> <ok:true|false> <detail>
    "$PY" -c "import sys; sys.path.insert(0,'$ROOT'); from clone_improve_lib import loud_fail; loud_fail('$1', $2, '''$3''', threshold=3, cooldown_h=24)" 2>/dev/null || true
}

STATUS=""   # "SG:ok TH:fail ..." 集約

# refresh_country <code> <fetch_cmds...> は複雑なので国別関数で明示

run_step() { # run_step <label> <cmd...>  失敗で非0
    log "  > $1"
    if ! "${@:2}"; then log "  ✗ $1 失敗"; return 1; fi
}

# ---------------- Singapore ----------------
refresh_sg() {
    run_step "sg fetch_stores" "$PY" whitespace_analysis_sg/fetch_stores_sg.py || return 1
    run_step "sg fetch_osm"    "$PY" whitespace_analysis_sg/fetch_osm_sg.py    || return 1
    run_step "sg build_matrix" "$PY" whitespace_analysis_sg/build_matrix_sg.py || return 1
    "$PY" - <<'PYEOF' || return 2
import csv,sys
ind=list(csv.DictReader(open("output_sg/raw/indicators_sg.csv")))
od=sum(int(r["owndays"]) for r in ind); lk=sum(int(r["lenskart"]) for r in ind); ml=sum(int(r["malls"]) for r in ind)
ok = od>=30 and lk>=20 and ml>=150
print(f"  sanity SG: OWNDAYS={od} Lenskart={lk} malls={ml} -> {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
PYEOF
    run_step "sg make_html" "$PY" whitespace_analysis_sg/make_interactive_sg.py || return 1
    cp output_sg/scatter_interactive_sg.html data/brain/web/scatter_interactive_sg.html || return 1
}

# ---------------- Thailand ----------------
refresh_th() {
    run_step "th fetch_stores" "$PY" whitespace_analysis_th/fetch_stores_th.py || return 1
    run_step "th fetch_osm"    "$PY" whitespace_analysis_th/fetch_osm_th.py    || return 1
    run_step "th build_matrix" "$PY" whitespace_analysis_th/build_matrix_th.py || return 1
    "$PY" - <<'PYEOF' || return 2
import csv,sys
ind=list(csv.DictReader(open("output_th/raw/indicators_th.csv")))
od=sum(int(r["owndays"]) for r in ind); lk=sum(int(r["lenskart"]) for r in ind); ml=sum(int(r["malls"]) for r in ind)
ok = od>=50 and lk>=10 and ml>=300
print(f"  sanity TH: OWNDAYS={od} Lenskart={lk} malls={ml} -> {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
PYEOF
    run_step "th make_html" "$PY" whitespace_analysis_th/make_interactive_th.py || return 1
    cp output_th/scatter_interactive_th.html data/brain/web/scatter_interactive_th.html || return 1
}

# ---------------- Taiwan ----------------
refresh_tw() {
    for f in fetch_owndays_tw fetch_formosa_tw fetch_kobayashi_tw fetch_jins_tw fetch_meganeichiba_tw fetch_klassic_tw; do
        run_step "tw $f" "$PY" "whitespace_analysis_tw/$f.py" || return 1
    done
    run_step "tw fetch_facilities" "$PY" whitespace_analysis_tw/fetch_facilities_tw.py --refetch || return 1
    run_step "tw build_matrix" "$PY" whitespace_analysis_tw/build_matrix_tw.py || return 1
    "$PY" - <<'PYEOF' || return 2
import csv,sys
ind=list(csv.DictReader(open("output_tw/raw/indicators_tw.csv")))
od=sum(int(r["owndays"]) for r in ind); fo=sum(int(r["formosa"]) for r in ind)
import os
nfac=sum(1 for _ in open("output_tw/raw/enrich/facilities_raw_tw.csv"))-1 if os.path.exists("output_tw/raw/enrich/facilities_raw_tw.csv") else 0
ok = od>=50 and fo>=200 and nfac>=100
print(f"  sanity TW: OWNDAYS={od} Formosa={fo} facilities={nfac} -> {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
PYEOF
    run_step "tw make_html" "$PY" whitespace_analysis_tw/make_interactive_tw.py || return 1
    cp output_tw/scatter_interactive_tw.html data/brain/web/scatter_interactive_tw.html || return 1
}

# ---------------- Japan (重量: ~30-45分。全チェーン再scrape + geocode + malls + 多段enrich) ----------------
refresh_jp() {
    run_step "jp scrape_chains"  "$PY" whitespace_analysis/scrape_chains.py owndays zoff parismiki meganeichiba aigan alook || return 1
    run_step "jp scrape_jins"    "$PY" whitespace_analysis/scrape_jins.py     || return 1
    run_step "jp fetch_coords"   "$PY" whitespace_analysis/fetch_coords.py    || return 1
    run_step "jp geocode_chains" "$PY" whitespace_analysis/geocode_chains.py  || return 1
    run_step "jp fetch_malls"    "$PY" whitespace_analysis/fetch_malls.py     || return 1
    run_step "jp fetch_opticians" "$PY" whitespace_analysis/fetch_opticians.py || return 1
    run_step "jp build_matrix"   "$PY" whitespace_analysis/build_matrix.py    || return 1
    "$PY" - <<'PYEOF' || return 2
import csv,os,sys
ind=list(csv.DictReader(open("output/raw/indicators.csv")))
od=sum(int(float(r["owndays"])) for r in ind); ji=sum(int(float(r["jins"])) for r in ind)
ml=sum(1 for _ in open("output/raw/enrich/commercial_facilities.csv"))-1 if os.path.exists("output/raw/enrich/commercial_facilities.csv") else 0
ok = od>=200 and ji>=400 and ml>=2000
print(f"  sanity JP: OWNDAYS={od} JINS={ji} malls={ml} -> {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
PYEOF
    run_step "jp catchment"    "$PY" whitespace_analysis/catchment_add.py     || return 1
    run_step "jp age"          "$PY" whitespace_analysis/age_add.py           || return 1
    run_step "jp enrich_join"  "$PY" whitespace_analysis/enrich_join.py       || return 1
    run_step "jp station_500m" "$PY" whitespace_analysis/build_station_500m.py || return 1
    run_step "jp store_names"  "$PY" whitespace_analysis/build_store_names.py  || return 1
    # メタデータ補完 (①眼科 OSM / ⑥世帯構成)。非致命=失敗しても前回値維持・dashboard は継続
    run_step "jp eye_clinics"  "$PY" whitespace_analysis/fetch_eye_clinics.py  || log "  (eye_clinics skip: OSM 失敗 → 前回値維持)"
    run_step "jp enrich_meta"  "$PY" whitespace_analysis/enrich_metadata.py    || log "  (enrich_metadata skip)"
    # 売上予測(仮): 歴年売上 + 類似店 analog を再生成(dashboard 予測パネルが embed する実データ)。非致命=失敗時は前回 json 維持
    run_step "jp sales_hist"   "$PY" whitespace_analysis/build_store_sales_history.py || log "  (sales_history skip)"
    run_step "jp sales_pred"   "$PY" whitespace_analysis/build_sales_predictions.py    || log "  (sales_predictions skip: 前回 json 維持)"
    run_step "jp make_html"    "$PY" whitespace_analysis/make_interactive.py  || return 1
    cp output/scatter_interactive.html data/brain/web/scatter_interactive.html || return 1
}

# 引数で国を絞れる (例: whitespace_refresh.sh jp = JP のみ)。無指定は全4国。
COUNTRIES=("$@"); [ ${#COUNTRIES[@]} -eq 0 ] && COUNTRIES=(sg th tw jp)
for C in "${COUNTRIES[@]}"; do
    log "===== $C ====="
    if "refresh_$C"; then
        STATUS="$STATUS $C:ok"; _loud "whitespace_refresh_$C" True "$C 更新成功"
    else
        rc=$?; STATUS="$STATUS $C:FAIL(rc=$rc)"
        _loud "whitespace_refresh_$C" False "$C 月次更新 失敗 (rc=$rc: 1=fetch/build, 2=sanity gate=Overpass空/店舗数異常)。HTML は前回維持。"
    fi
done
log "結果:$STATUS"

# --- 生成 HTML に差分があれば commit + push (surgical、rebase retry で並行 git と両立) ---
if [ "${WHITESPACE_REFRESH_NOGIT:-0}" = "1" ]; then
    log "WHITESPACE_REFRESH_NOGIT=1 → git commit/push skip (test mode)"
elif [ -n "$(git status --porcelain data/brain/web/scatter_interactive.html data/brain/web/scatter_interactive_sg.html data/brain/web/scatter_interactive_th.html data/brain/web/scatter_interactive_tw.html)" ]; then
    git add data/brain/web/scatter_interactive.html data/brain/web/scatter_interactive_sg.html data/brain/web/scatter_interactive_th.html data/brain/web/scatter_interactive_tw.html
    git commit -m "chore(whitespace): 月次 店舗+施設データ更新 $(date +%Y-%m) [$STATUS ]" 2>&1 | tail -2
    for i in 1 2 3; do
        git fetch origin main --quiet 2>/dev/null && git rebase origin/main 2>&1 | tail -1
        if git push origin HEAD:main 2>&1 | tail -2; then log "push ok"; break; fi
        log "push retry $i"; sleep 10
    done
else
    log "HTML 差分なし (push 不要)"
fi
log "done"

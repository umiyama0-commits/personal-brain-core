#!/usr/bin/env python3
"""scripts/magazine_backfill.py — もぐもぐダイアリー過去号 backfill の遠隔実行 (★2026-07-06 海山「2も進めて」)

背景: 過去号の人格取込 (stapa 全号再取得 → magazine_persona_ingest で 15 号ずつ蒸留) は
Mac Studio での手動 3 コマンドを想定していたが、リモート session からは stapa 非到達で
実行できない。Mac mini は stapa cron (STAPA_USER/PASS ログイン) + LITELLM を既に持つため、
**git 配送のリクエストファイル**を本 orchestrator (hourly cron) が拾って自動実行する。

★2026-07-06 workflow レビュー (3 レンズ × 敵対検証、confirmed 12 件) 反映後の設計:
  - scrape は `--output-dir data/brain/raw/magazine_backfill/` の **side-dir 直書き** —
    IMPORT_DIR に数百 file を投下しない (= watcher の Opus compile 数百回・import 経路の
    数時間閉塞・既取込号の重複 re-compile を全て回避)。ingest も同 dir を読むため
    scrape→蒸留が同期になり「watcher 未到達 → 偽完了」の race が構造的に消える
  - 完了判定は ingest の明示キー `pending_todo == 0` (+ total>0, failed==0) のみ —
    remaining 欠落を 0 と読む偽完了を排除
  - ingest の `failed` (LLM 断で error dict の号) は done にせず retry、_loud(False) で通知
  - request JSON の parse 失敗 / 型不正は「request なし」と区別して loud (§1.18)
  - lock は pid 生存確認 (死んだ持ち主は即回収)、subprocess timeout 3h、stale fallback 6h
  - レビュー滞留ゲートは applied_partial (accept-all 後の low/推測 残渣) を数えない +
    gate 連続 skip 15 cycle (≈1 日) ごとに nudge push (無音停止しない)

プロトコル:
  - リクエスト (git 追跡): data/brain/magazine_backfill_request.json
      {"requested_at": "YYYY-MM-DD", "batch_limit": 15, "pending_gate": 10}
    requested_at を変えて push すると新しい backfill として再実行される。
  - 状態 (git 非管理、Mac mini local): data/brain/.magazine_backfill_state.json

実行:
  python3 scripts/magazine_backfill.py --dry-run   # 今 cycle で何をするかの表示のみ
  python3 scripts/magazine_backfill.py             # 1 cycle 実行 (cron が呼ぶ)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("magazine_backfill")

JST = timezone(timedelta(hours=9))
BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", ROOT / "data" / "brain"))
REQUEST_FILE = BRAIN_ROOT / "magazine_backfill_request.json"
STATE_FILE = BRAIN_ROOT / ".magazine_backfill_state.json"
LOCK_FILE = BRAIN_ROOT / ".magazine_backfill.lock"
# scrape の side-dir (IMPORT_DIR 非経由 = LLM compile に乗せない、ingest が直接読む)
BACKFILL_DIR = BRAIN_ROOT / "raw" / "magazine_backfill"
EXTRACTED_DIR = BRAIN_ROOT / "alignment" / "interview_extracted"
LOCK_STALE_SEC = 6 * 3600          # pid 不明時の fallback (pid 生存確認が第一判定)
SCRAPE_TIMEOUT_SEC = 3 * 3600      # Playwright ハング対策
GATE_NUDGE_EVERY = 15              # gate 連続 skip の nudge 間隔 (≈1 日 = 8-22 時の 15 cycle)

_MISSING = object()


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _load(p: Path):
    """dict / None (parse 失敗) / _MISSING (file 不在) を区別して返す。"""
    if not p.exists():
        return _MISSING
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _loud(ok: bool, detail: str = "") -> None:
    """§1.18: scrape/蒸留の silent 死防止 (成功で streak リセット)。"""
    try:
        from clone_improve_lib import loud_fail
        loud_fail("magazine_backfill", ok, detail, threshold=2, cooldown_h=12)
    except Exception:
        pass


def _push(text: str) -> None:
    try:
        from clone_improve_lib import line_push_digest
        line_push_digest(text, "magazine")
    except Exception as e:
        logger.warning(f"LINE push 失敗 (処理は継続): {e}")


def _pending_magazine_reviews() -> int:
    """レビュー待ちの magazine 由来抽出の件数 (滞留ゲート判定)。

    ★applied_partial 付き = accept-all 済みで low/『推測:』残渣だけが残る file は数えない
    (workflow レビュー MAJOR: 残渣は設計上 pending に残り続けるため、数えると 1-2 batch で
    gate が恒久閉塞し backfill が無音停止する)。"""
    n = 0
    if not EXTRACTED_DIR.is_dir():
        return 0
    for f in EXTRACTED_DIR.glob("magazine-*.json"):
        d = _load(f)
        if not isinstance(d, dict):
            continue
        if (d.get("status") == "pending_review" and d.get("source") == "magazine"
                and "applied_partial" not in d):
            n += 1
    return n


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _acquire_lock() -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        # 第一判定: 持ち主 pid の生存 (mtime だけだと長時間 scrape 中の生きた lock を
        # 「stale」誤回収して二重起動する = workflow レビュー MINOR)
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            pid = 0
        if pid and _pid_alive(pid):
            return False
        if not pid and time.time() - LOCK_FILE.stat().st_mtime < LOCK_STALE_SEC:
            return False
        logger.warning(f"死んだ持ち主の lock を回収 (pid={pid})")
        LOCK_FILE.unlink(missing_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _run_scrape() -> bool:
    """stapa_scraper --all --output-dir (side-dir) を 1 回実行。"""
    logger.info(f"stapa_scraper --all 開始 (全号再取得 → {BACKFILL_DIR})")
    try:
        rc = subprocess.run(
            [sys.executable, str(ROOT / "stapa_scraper.py"), "--all",
             "--output-dir", str(BACKFILL_DIR)],
            cwd=str(ROOT), timeout=SCRAPE_TIMEOUT_SEC,
        ).returncode
    except subprocess.TimeoutExpired:
        logger.error(f"stapa_scraper --all timeout ({SCRAPE_TIMEOUT_SEC}s)")
        return False
    logger.info(f"stapa_scraper --all 終了 rc={rc}")
    if rc != 0:
        return False
    # rc=0 でも出力ゼロ (ログイン失敗等の縮退) は成功にしない
    n = len(list(BACKFILL_DIR.glob("mogumog_*.txt"))) + len(list(BACKFILL_DIR.glob("onmaga_batch_*.txt")))
    if n == 0:
        logger.error("scrape rc=0 だが出力 0 file (ログイン失敗/縮退の疑い)")
        return False
    return True


def _parse_request(req: dict) -> dict | None:
    """request の型検証 (git 配送 = 人が編集するので不正値は loud に弾く)。"""
    try:
        return {
            "request_id": str(req["requested_at"]),
            "batch_limit": int(req.get("batch_limit") or 15),
            "pending_gate": int(req.get("pending_gate") or 10),
        }
    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"request 不正: {type(e).__name__}: {e}")
        return None


def run_cycle(dry_run: bool = False) -> dict:
    raw_req = _load(REQUEST_FILE)
    if raw_req is _MISSING:
        return {"ok": True, "note": "request なし"}
    if not isinstance(raw_req, dict) or not raw_req.get("requested_at"):
        # 壊れた JSON / 必須キー欠落を「request なし」と同一視しない (§1.18:
        # push したはずの backfill が無音で始まらない事故の防止)
        _loud(False, "magazine_backfill_request.json が parse 不能 or requested_at 欠落")
        return {"ok": False, "error": "request 不正 (parse/必須キー)"}
    req = _parse_request(raw_req)
    if req is None:
        _loud(False, "magazine_backfill_request.json の型不正 (batch_limit/pending_gate は整数)")
        return {"ok": False, "error": "request 型不正"}

    st = _load(STATE_FILE)
    if not isinstance(st, dict) or st.get("request_id") != req["request_id"]:
        st = {"request_id": req["request_id"], "scraped_at": "", "batches": 0,
              "extracted_total": 0, "completed_at": "", "gate_skips": 0}
    if st.get("completed_at"):
        return {"ok": True, "note": f"request {req['request_id']} は完了済み ({st['completed_at']})"}

    pending = _pending_magazine_reviews()
    gated = pending >= req["pending_gate"]
    if dry_run:
        plan = ([] if st.get("scraped_at") else [f"scrape --all → {BACKFILL_DIR}"])
        plan.append(f"蒸留 --limit {req['batch_limit']}"
                    + (f" (skip: レビュー待ち {pending} ≥ {req['pending_gate']})" if gated else ""))
        return {"ok": True, "dry_run": True, "request_id": req["request_id"], "plan": plan,
                "pending_reviews": pending}

    if not _acquire_lock():
        return {"ok": True, "note": "前 cycle 実行中 (lock) → skip"}
    try:
        # ① scrape (この request で 1 回だけ、side-dir へ)
        if not st.get("scraped_at"):
            if not _run_scrape():
                _loud(False, "stapa_scraper --all 失敗 (次 cycle retry)")
                return {"ok": False, "error": "scrape 失敗"}
            st["scraped_at"] = _now()
            _save_state(st)
            _push("📖 もぐもぐ backfill: stapa 全号の再取得が完了。ここから 15 号ずつ蒸留していきます")

        # ② レビュー滞留ゲート (trickle: 溜まってたら海山のレビュー待ち。無音停止はしない)
        if gated:
            st["gate_skips"] = int(st.get("gate_skips", 0)) + 1
            _save_state(st)
            if st["gate_skips"] % GATE_NUDGE_EVERY == 0:
                _push(f"📖 もぐもぐ backfill 待機中: レビュー待ち {pending} 件が捌けると次の "
                      f"{req['batch_limit']} 号へ進みます (/align-voice → まとめて採用 が早い)")
            _loud(True)
            return {"ok": True, "note": f"レビュー待ち {pending} 件 ≥ gate {req['pending_gate']} → 今 cycle は蒸留 skip"}
        st["gate_skips"] = 0

        # ③ 次の batch を蒸留 (scrape と同じ side-dir を読む = 同期、watcher 非依存)
        from magazine_persona_ingest import run as ingest_run
        r = asyncio.run(ingest_run(notes_dir=BACKFILL_DIR, limit=req["batch_limit"]))

        total = int(r.get("total", 0))
        extracted = int(r.get("extracted", 0))
        failed = int(r.get("failed", 0))
        pending_todo = r.get("pending_todo")  # 明示キー必須 (欠落を 0 と読まない)

        if total == 0:
            # scrape 済みなのに columns 0 = 本文抽出の縮退 (完了扱いにしない)
            _loud(False, f"backfill dir に diary コラム 0 件 ({BACKFILL_DIR})")
            return {"ok": False, "error": "columns 0 (scrape 縮退の疑い)"}
        if failed:
            _loud(False, f"蒸留失敗 {failed} 号 (LITELLM 断?) — 未 done なので次 cycle retry")
        if extracted:
            st["batches"] = int(st.get("batches", 0)) + 1
            st["extracted_total"] = int(st.get("extracted_total", 0)) + extracted
            _save_state(st)
            _push(f"📖 もぐもぐ backfill: {extracted} 号を蒸留 → /align-voice でレビューを。"
                  f"残り {pending_todo} 号 (レビューが進むと次の {req['batch_limit']} 号へ)")

        # ④ 完了は「未取込ゼロが明示され、失敗ゼロ」の時だけ
        if pending_todo == 0 and not failed:
            st["completed_at"] = _now()
            _save_state(st)
            _loud(True)
            _push(f"✅ もぐもぐ backfill 完了: 全 {total} 号を蒸留済み "
                  f"(この request での取込 {st['extracted_total']} 号)。レビューは /align-voice で")
            return {"ok": True, "completed": True, "total": total}

        if not failed:
            _loud(True)
        return {"ok": not failed, "extracted": extracted, "failed": failed,
                "pending_todo": pending_todo, "batches": st.get("batches", 0)}
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="もぐもぐダイアリー backfill orchestrator (git 配送)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        r = run_cycle(dry_run=a.dry_run)
    except Exception as e:
        # uncaught を scrape.log だけに沈めない (§1.18)
        logger.exception("run_cycle 例外")
        _loud(False, f"magazine_backfill 例外: {type(e).__name__}: {e}")
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

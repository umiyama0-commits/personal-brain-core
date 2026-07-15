"""
routes/alignment_trial.py — alignment_trial の HTML/review/run/status endpoint (★2026-05-22 Phase 2)

5 endpoint:
- GET  /alignment-trial/                    : run 一覧
- GET  /alignment-trial/{run_id}            : HTML レビュー UI
- POST /alignment-trial/{run_id}/review     : サーバ送信ボタンから review JSON 取り込み
- POST /alignment-trial/run                 : remote から run trigger (BackgroundTask 5-10 分)
- GET  /alignment-trial/{run_id}/status     : run の進行確認

注: 切り出し前は main.py に直接 `@app.get` で 4 つ + main.py 別箇所に古い `/alignment-trial/{run_id}` 重複 1 つ。
重複は後勝ち動作 (FastAPI 仕様) のため、切り出し時に新版だけ移行。

依存:
- request.app.state.brain (= BrainWiki インスタンス)
- /app/scripts/clone_alignment_trial.py (= parse_questions / run_trial / load_run / save_run / ingest_review)
- /app/data/brain/clone_improve/alignment_trial/runs/ (= _AT_RUNS_DIR)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

# ─── shared constants & validators ─────────────
# ★fix 2026-05-25 BLOCKER B-1: hardcode default を削除。過去 git 公開済の token を
# default にしていたため、誰でも tracked code を読めば取得可能 = 完全 leak だった。
# fallback で VOICE_ALIGN_TOKEN を許容 (同じ「海山さん専用内部 UI」枠で運用)、
# どちらも未設定なら check_at_token() が 503 で fail-closed。
ALIGNMENT_TRIAL_TOKEN = os.getenv(
    "ALIGNMENT_TRIAL_TOKEN", os.getenv("VOICE_ALIGN_TOKEN", "")
)
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
AT_RUNS_DIR = Path("/app/data/brain/clone_improve/alignment_trial/runs")


# ★2026-06-08 システム評価 0-2: deploy/rebuild 系 (docker socket 経由 = RCE 相当) を閲覧 UI と
# 別 token に分離。DEPLOY_ADMIN_TOKEN があればそれを要求、無ければ ALIGNMENT_TRIAL_TOKEN に fallback
# (= backward-compat、海山が .env に DEPLOY_ADMIN_TOKEN を入れるまで現行 token で動く=ロックアウト無し)。
DEPLOY_ADMIN_TOKEN = os.getenv("DEPLOY_ADMIN_TOKEN", "")


def _log_auth_denied(action: str, token: str) -> None:
    """★2026-06-08 評価 Security: 認証失敗 (403) を bot_events に記録 (brute-force/侵入検知)。

    従来は 403 を投げるだけで「誰が叩いて弾かれたか」が残らずスキャン検知に使えなかった。
    失敗 event を append-only 記録し、bot_uptime_monitor が burst を検知して alert する。
    本流を絶対に壊さない (try/except 全包み)。
    """
    try:
        import sys as _sys
        import hashlib
        from pathlib import Path as _Path
        _scripts = str(_Path(__file__).resolve().parent.parent / "scripts")
        if _scripts not in _sys.path:
            _sys.path.insert(0, _scripts)
        from bot_events import log_bot_event  # type: ignore
        tok_id = hashlib.sha256((token or "").encode()).hexdigest()[:12] if token else "empty"
        log_bot_event("admin_audit", "auth_denied", action=action, token_id=tok_id, result="denied")
    except Exception:
        pass


def check_at_token(token: str) -> None:
    """token 認証 + 未設定検出 (★2026-06-08 評価 0-4: 定数時間比較)。"""
    if not ALIGNMENT_TRIAL_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ALIGNMENT_TRIAL_TOKEN not configured in .env",
        )
    if not hmac.compare_digest(token or "", ALIGNMENT_TRIAL_TOKEN):
        _log_auth_denied("view_token", token)
        raise HTTPException(status_code=403, detail="invalid token")


def check_deploy_token(token: str) -> None:
    """deploy/rebuild 用の token 認証 (閲覧 token と分離可能、定数時間比較)。"""
    expected = DEPLOY_ADMIN_TOKEN or ALIGNMENT_TRIAL_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="deploy token not configured")
    if not hmac.compare_digest(token or "", expected):
        _log_auth_denied("deploy_token", token)
        raise HTTPException(status_code=403, detail="invalid deploy token")


def check_at_run_id(run_id: str) -> None:
    """path traversal 防止 (= alphanumeric + dash/underscore のみ)。"""
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="invalid run_id")


# ─── APIRouter ─────────────
router = APIRouter(tags=["alignment_trial"])


@router.get("/alignment-trial/")
async def alignment_trial_index(token: str = Query(...)):
    """alignment-trial の run 一覧 (= run_*.html を listing)。"""
    check_at_token(token)
    if not AT_RUNS_DIR.exists():
        return {"runs": []}
    runs = []
    for f in sorted(AT_RUNS_DIR.glob("*.html")):
        runs.append({
            "run_id": f.stem,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    return {"runs": runs}


@router.get("/alignment-trial/{run_id}")
async def alignment_trial_view(run_id: str, token: str = Query(...)):
    """135 件 alignment trial の HTML レビュー UI を返す。"""
    check_at_token(token)
    check_at_run_id(run_id)
    html_path = AT_RUNS_DIR / f"{run_id}.html"
    if not html_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"run not found: {run_id}.html",
        )
    return Response(
        content=html_path.read_text(encoding="utf-8"),
        media_type="text/html; charset=utf-8",
    )


@router.post("/alignment-trial/{run_id}/review")
async def alignment_trial_review(
    request: Request,
    run_id: str,
    token: str = Query(...),
):
    """browser の「サーバ送信」ボタンから review JSON を受け取り、即 ingest。"""
    check_at_token(token)
    check_at_run_id(run_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    AT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y-%m-%d_%H%M%S")
    review_path = AT_RUNS_DIR / f"{run_id}_review_{ts}.json"
    try:
        review_path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"write failed: {e}")

    # ingest_review を呼ぶ
    summary: dict = {}
    try:
        import sys as _at_sys
        if "/app/scripts" not in _at_sys.path:
            _at_sys.path.insert(0, "/app/scripts")
        from clone_alignment_trial import ingest_review as _at_ingest  # type: ignore
        summary = _at_ingest(review_path, run_id)
    except Exception as e:
        logger.warning(f"alignment_trial ingest failed: {e}")
        return {
            "status": "saved_but_not_ingested",
            "saved_to": str(review_path),
            "error": str(e),
        }

    return {
        "status": "ok",
        "saved_to": str(review_path),
        "summary": summary,
    }


@router.post("/alignment-trial/run")
async def alignment_trial_run(
    request: Request,
    bg_tasks: BackgroundTasks,
    token: str = Query(...),
):
    """remote から alignment_trial の run を triggered。

    body: {
      "run_id": "2026-05-21_run2",       # 必須
      "base": "2026-05-21_run1",          # 任意 (= rerun base、未指定なら questions.md から新規)
      "tag": "v1正式公開前 v2",            # 任意
      "use_prefix": true,                 # 任意 (default True、TRIAL_PROMPT_PREFIX を使うか)
      "model": "smart",                   # 任意
      "max_concurrency": 3                # 任意 (rate limit 回避、1-5 クランプ)
    }
    response: { "status": "started", "run_id": ... }
    実行は BackgroundTask で非同期、5-10 分後に HTML 生成。
    完了確認は GET /alignment-trial/<run_id>/status
    """
    check_at_token(token)
    try:
        body = await request.json()
    except Exception:
        body = {}
    run_id = body.get("run_id", datetime.now(JST).strftime("%Y-%m-%d_%H%M"))
    base = body.get("base")
    tag = body.get("tag", "v1")
    use_prefix = bool(body.get("use_prefix", True))
    model = body.get("model", "smart")
    # ★2026-05-22: max_concurrency を body から受ける (rate limit 回避用、default 3、1-5 クランプ)
    try:
        max_concurrency = int(body.get("max_concurrency", 3))
    except Exception:
        max_concurrency = 3
    max_concurrency = max(1, min(max_concurrency, 5))

    check_at_run_id(run_id)
    if base:
        check_at_run_id(base)

    # app reference を closure に hold (request.app は閉じてる可能性があるので)
    app = request.app

    async def _execute():
        try:
            import sys as _at_sys
            if "/app/scripts" not in _at_sys.path:
                _at_sys.path.insert(0, "/app/scripts")
            from clone_alignment_trial import (  # type: ignore
                parse_questions as _parse,
                run_trial as _trial_run,
                load_run as _load,
                save_run as _save,
            )
            if base:
                base_run = _load(base)
                questions = [
                    {k: r[k] for k in
                     ("id", "role", "category", "scenario", "expected_axes")}
                    for r in base_run["results"]
                ]
                logger.info(f"alignment trial: rerun base={base}, {len(questions)} questions")
            else:
                questions = _parse()
                logger.info(f"alignment trial: fresh run, {len(questions)} questions")
            results = await _trial_run(
                questions, model=model, use_prefix=use_prefix,
                brain_wiki=app.state.brain,
                max_concurrency=max_concurrency,
            )
            _save(results, run_id, tag=tag)
            logger.info(
                f"alignment trial run {run_id} completed: "
                f"{len(results)} results saved"
            )
        except Exception as e:
            logger.exception(f"alignment trial run {run_id} failed")
            error_path = AT_RUNS_DIR / f"{run_id}_error.json"
            try:
                AT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
                error_path.write_text(
                    json.dumps({"error": str(e),
                                "ts": datetime.now(JST).isoformat()},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

    bg_tasks.add_task(_execute)
    return {
        "status": "started",
        "run_id": run_id,
        "base": base,
        "tag": tag,
        "use_prefix": use_prefix,
        "model": model,
        "max_concurrency": max_concurrency,
        "note": "実行は backgroundで進行。完了確認は GET /alignment-trial/{run_id}/status",
        "view_url_after_completion":
            f"/alignment-trial/{run_id}?token=<TOKEN>",
    }


@router.get("/alignment-trial/{run_id}/status")
async def alignment_trial_status(run_id: str, token: str = Query(...)):
    """run の進行状況確認 (= JSON/HTML が生成済か、error あるか)。"""
    check_at_token(token)
    check_at_run_id(run_id)
    json_path = AT_RUNS_DIR / f"{run_id}.json"
    html_path = AT_RUNS_DIR / f"{run_id}.html"
    error_path = AT_RUNS_DIR / f"{run_id}_error.json"

    if error_path.exists():
        try:
            err = json.loads(error_path.read_text(encoding="utf-8"))
        except Exception:
            err = {"error": "(parse failed)"}
        return {"status": "error", **err}

    if json_path.exists() and html_path.exists():
        try:
            run = json.loads(json_path.read_text(encoding="utf-8"))
            n = len(run.get("results", []))
        except Exception:
            n = -1
        return {
            "status": "complete",
            "n_results": n,
            "json_size_kb": round(json_path.stat().st_size / 1024, 1),
            "html_size_kb": round(html_path.stat().st_size / 1024, 1),
            "completed_at": datetime.fromtimestamp(
                html_path.stat().st_mtime).isoformat(),
            "view_url": f"/alignment-trial/{run_id}?token=<TOKEN>",
        }

    return {"status": "running_or_not_started",
            "run_id": run_id,
            "hint": "まだ実行中、あるいはこの run_id では未実行。1-2 分待って再 status。"}

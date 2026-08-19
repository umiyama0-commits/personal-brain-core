"""
routes/brain_api.py — Brain API endpoint (★2026-05-22 Phase 2)

2 endpoint:
- GET /api/cost-investigation : LLM 利用統計 (bot_events + extractor events + LiteLLM /spend)
- GET /api/recent-failures    : 直近 turn_failed event の error 詳細

★2026-05-22 緊急追加: 海山「全応答 fail」報告 (= 429 連発) の原因究明用。
切り出し時に APIRouter 化、token は alignment_trial と共通。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .alignment_trial import check_at_token, check_deploy_token

JST = ZoneInfo("Asia/Tokyo")

# LiteLLM proxy URL (= main.py から渡してもよいが、env で十分)
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")  # ★平文 default 禁止 (LEE §3.1)

router = APIRouter(tags=["brain_api"])


def _audit_privileged(request: Request, action: str, token: str,
                      *, target: str = "", result: str = "authorized") -> None:
    """特権操作 (RCE 相当: redeploy/rebuild/reindex) の監査ログ。

    ★2026-06-08 システム評価 Security HIGH-3: 特権 endpoint に監査ログが皆無で、
    「誰が・いつ・どの token で host docker を叩いたか」の痕跡が残らなかった (SOC2 CC7 gap)。
    token は sha256 短縮 hash で記録し平文を残さない。本流を絶対に壊さない (try/except)。
    """
    try:
        import hashlib
        from bot_events import log_bot_event as _log  # type: ignore
        src_ip = ""
        try:
            src_ip = (request.headers.get("cf-connecting-ip")
                      or request.headers.get("x-forwarded-for")
                      or (request.client.host if request.client else ""))
        except Exception:
            pass
        tok_id = hashlib.sha256((token or "").encode()).hexdigest()[:12] if token else "none"
        _log("admin_audit", action, target=target, src_ip=src_ip,
             token_id=tok_id, result=result)
    except Exception:
        pass


@router.get("/api/cost-investigation")
async def cost_investigation(token: str = Query(...), days: int = 7):
    """直近 N 日の LLM 利用統計を集計。

    GET /api/cost-investigation?token=<TOKEN>&days=7

    response:
      {
        "window_days": 7,
        "bot_events": { component_name: { started, ok, failed, total_ms } },
        "bot_events_total": int,
        "extractors": { extractor_name: { start, finish, fail, total_sec } },
        "extractor_total": int,
        "litellm_spend": (LiteLLM の usage 集計、取れれば),
        "estimate": {
          "rough_calls_per_day": int,
          "top_consumers": [...]
        }
      }
    """
    check_at_token(token)

    result: dict = {
        "window_days": days,
        "ts": datetime.now(JST).isoformat(),
    }

    # ─── 1. bot_events.jsonl 集計 ───
    bot_total = 0
    by_comp: dict = {}
    try:
        import sys as _ci_sys
        if "/app/scripts" not in _ci_sys.path:
            _ci_sys.path.insert(0, "/app/scripts")
        from bot_events import iter_events as _iter  # type: ignore
        events = list(_iter(since_sec=days * 86400))
        bot_total = len(events)
        for e in events:
            c = e.get("component", "?")
            ev = e.get("event")
            d = by_comp.setdefault(c, {"started": 0, "ok": 0, "failed": 0,
                                        "skipped": 0, "total_ms": 0})
            if ev == "turn_started":
                d["started"] += 1
            elif ev == "turn_finished":
                d["ok"] += 1
                try:
                    d["total_ms"] += int(e.get("elapsed_ms", 0))
                except Exception:
                    pass
            elif ev == "turn_failed":
                d["failed"] += 1
            elif ev in ("turn_skipped", "user_skipped"):
                d["skipped"] += 1
        # avg_ms 計算
        for c, d in by_comp.items():
            d["avg_ms"] = round(d["total_ms"] / d["ok"], 0) if d["ok"] else 0
        result["bot_events"] = by_comp
        result["bot_events_total"] = bot_total
    except Exception as e:
        result["bot_events_error"] = str(e)

    # ─── 2. extractor events.jsonl 集計 ───
    ex_by: dict = {}
    try:
        ev_log = Path("/app/data/brain/extractor_state/events.jsonl")
        if ev_log.exists():
            cutoff_dt = datetime.now() - timedelta(days=days)
            cutoff = cutoff_dt.isoformat()
            for line in ev_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("ts", "") < cutoff:
                    continue
                ext = e.get("extractor", "?")
                ev = e.get("event")
                d = ex_by.setdefault(ext, {"start": 0, "finish": 0, "fail": 0,
                                            "llm_call_failed": 0, "total_sec": 0.0})
                if ev == "run_started":
                    d["start"] += 1
                elif ev == "run_finished":
                    d["finish"] += 1
                    try:
                        d["total_sec"] += float(e.get("elapsed_sec", 0))
                    except Exception:
                        pass
                elif ev == "run_failed":
                    d["fail"] += 1
                elif ev == "llm_call_failed":
                    d["llm_call_failed"] += 1
            for ext, d in ex_by.items():
                d["avg_sec"] = round(d["total_sec"] / d["finish"], 1) if d["finish"] else 0
            result["extractors"] = ex_by
    except Exception as e:
        result["extractors_error"] = str(e)

    # ─── 3. LiteLLM /spend を proxy で取得 ───
    try:
        async with httpx.AsyncClient(timeout=10.0) as _http:
            # 試行 1: /spend/logs (= LiteLLM v1.30+、最近の call ログ)
            r = await _http.get(
                f"{LITELLM_URL}/spend/logs",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                params={"limit": 200},
            )
            if r.status_code == 200:
                result["litellm_spend_logs"] = r.json()
            else:
                # 試行 2: /usage/per_model
                r2 = await _http.get(
                    f"{LITELLM_URL}/usage/per_model",
                    headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                )
                if r2.status_code == 200:
                    result["litellm_usage_per_model"] = r2.json()
                else:
                    result["litellm_endpoint_status"] = {
                        "spend_logs": r.status_code,
                        "usage_per_model": r2.status_code,
                    }
    except Exception as e:
        result["litellm_error"] = str(e)

    # ─── 4. top consumer 推定 ───
    top: list = []
    for c, d in by_comp.items():
        top.append({"source": f"bot:{c}", "calls": d.get("ok", 0) + d.get("failed", 0)})
    for ext, d in ex_by.items():
        top.append({"source": f"extractor:{ext}", "calls": d.get("finish", 0) + d.get("fail", 0)})
    top.sort(key=lambda x: -x["calls"])
    result["estimate"] = {
        "top_consumers": top[:10],
        "rough_total_runs": sum(t["calls"] for t in top),
        "days": days,
    }

    return result


@router.get("/api/recent-failures")
async def recent_failures(token: str = Query(...), limit: int = 30):
    """直近の bot_events.jsonl から failed event を抽出して error 詳細を返す。

    海山が「すべての応答が fail」と報告 (2026-05-22)、原因究明用。
    """
    check_at_token(token)
    try:
        import sys as _ci_sys
        if "/app/scripts" not in _ci_sys.path:
            _ci_sys.path.insert(0, "/app/scripts")
        from bot_events import _events_log_path  # type: ignore
        path = _events_log_path()
        if not path.exists():
            return {"failures": [], "note": "events.jsonl が存在しない"}
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("event") == "turn_failed":
                events.append(e)
        # 最新 N 件
        events = events[-limit:]
        # error_class の集計
        by_error: dict = {}
        by_component: dict = {}
        for e in events:
            ec = e.get("error_class", "?")
            comp = e.get("component", "?")
            by_error[ec] = by_error.get(ec, 0) + 1
            by_component[comp] = by_component.get(comp, 0) + 1
        return {
            "n_failures": len(events),
            "by_error_class": by_error,
            "by_component": by_component,
            "recent_examples": [
                {
                    "ts": e.get("ts"),
                    "component": e.get("component"),
                    "model": e.get("model"),
                    "error_class": e.get("error_class"),
                    "error_msg": (e.get("error_msg") or "")[:300],
                    "user_id": e.get("user_id", "")[:8],
                }
                for e in events[-10:]
            ],
        }
    except Exception as e:
        return {"error": str(e)}


# ─── prompt diff check (★2026-05-23 海山指示) ────────────────────
# B (prompt cache 階層化) の品質影響を Mac Studio 不要で確認するための endpoint。
# 既存 scripts/clone_prompt_diff_check.py を brain.example.com 経由で発火 + 結果取得。
_REGRESSION_DIR = Path("/app/data/brain/clone_improve/regression")


@router.post("/api/prompt-diff/run")
async def prompt_diff_run(
    bg_tasks: BackgroundTasks,
    token: str = Query(...),
    trigger_sha: str = Query("manual", description="commit SHA or 任意のラベル"),
):
    """clone_prompt_diff_check.py を BackgroundTask で発火。

    動作:
      1. 既存 nightly regression (= 03:30 daily の `regression/YYYY-MM-DD.json`) を baseline に
      2. post-deploy regression を新 prompt で再走 (= 30 質問、smart で応答)
      3. pre/post を比較、`regression/diff-<sha>-<date>.json` に保存
      4. degraded ≥ 3 件で LINE Push (= 既存仕組み)

    所要: 5-10 分 (= 30 質問 × smart 呼出)。結果は GET /api/prompt-diff/latest で取得可。
    """
    check_at_token(token)
    # path traversal 防止
    if not all(c.isalnum() or c in "_-" for c in trigger_sha):
        raise HTTPException(status_code=400, detail="invalid trigger_sha (alphanumeric + _-)")

    async def _execute():
        try:
            import sys as _sys
            if "/app/scripts" not in _sys.path:
                _sys.path.insert(0, "/app/scripts")
            from clone_prompt_diff_check import main as _diff_main  # type: ignore
            # main() は argparse 使うので sys.argv を細工
            orig_argv = list(_sys.argv)
            _sys.argv = ["clone_prompt_diff_check.py", trigger_sha]
            try:
                rc = await _diff_main()
            finally:
                _sys.argv = orig_argv
            return rc
        except Exception as e:
            # logging は内部 logger に任せる、ここでは silent (= BackgroundTask で例外伝播しても意味ない)
            import logging
            logging.getLogger("prompt_diff_run").exception(f"failed: {e}")

    bg_tasks.add_task(_execute)
    return {
        "status": "started",
        "trigger_sha": trigger_sha,
        "note": "5-10 分で完了、結果は GET /api/prompt-diff/latest?token=... で取得",
        "expected_output": f"{_REGRESSION_DIR}/diff-{trigger_sha[:7]}-<today>.json",
    }


@router.get("/api/fine-tune/raw-file-preview")
async def fine_tune_raw_file_preview(
    token: str = Query(...),
    path: str = Query(..., description="data/brain/ 配下の rel path"),
    lines: int = Query(50, description="先頭 N 行"),
):
    """raw/ 配下の 1 file の先頭 N 行を返す (= format 確認用 debug)。

    海山「plaud_speaker / imported_drive 真因究明」を受けて新設。
    plaud transcript の speaker label format 判明後 iter 修正に繋げる。
    """
    check_at_token(token)
    BRAIN = Path("/app/data/brain")
    # 安全のため data/brain/ 配下のみ許可
    target = (BRAIN / path).resolve()
    try:
        target.relative_to(BRAIN.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path must be under data/brain/")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read error: {e}")
    head_lines = content.splitlines()[:lines]
    return {
        "path": str(target),
        "size_kb": round(target.stat().st_size / 1024, 1),
        "n_lines_total": len(content.splitlines()),
        "n_lines_returned": len(head_lines),
        "head": head_lines,
    }


@router.post("/api/admin/redeploy")
async def admin_redeploy(
    request: Request,
    token: str = Query(...),
    target: str = Query("line-bot", description="rebuild 対象 (= line-bot / litellm / all)"),
    confirm: str = Query("", description="安全確認: 'yes' 必須"),
):
    """MacBook curl で line-bot を rebuild + force-recreate (★2026-05-24 海山「MacBook 側で進める」)。

    前提:
    - docker-compose.yml で `/var/run/docker.sock:/var/run/docker.sock` mount 済
      (= 海山が初回 Mac Studio で 1 度 force-recreate 後、以降 MacBook 完結)
    - confirm=yes 必須 (= 安全 gate、誤実行防止、CLAUDE.md 1.3 destructive 配慮)

    動作:
    1. cd /app && git pull origin main (= 最新 code 取得)
    2. docker compose build {target} (= 再 build、Python 変更反映)
    3. docker compose up -d --force-recreate {target} (= TZ 等 env 変更反映)

    Returns:
      {"status": "completed", "steps": [...], "stdout_tail": [...], "exit_code": 0}

    使用例:
      curl -X POST "https://brain.example.com/api/admin/redeploy?token=...&confirm=yes" \\
        | python3 -m json.tool
    """
    check_deploy_token(token)  # ★2026-06-08 評価 0-2: deploy 系は閲覧 token と分離可能 (DEPLOY_ADMIN_TOKEN)
    _audit_privileged(request, "redeploy", token, target=target)  # ★評価 Security HIGH-3: 監査ログ

    if confirm != "yes":
        raise HTTPException(
            status_code=400,
            detail="confirm=yes 必須 (= destructive op、誤実行防止)",
        )
    if target not in ("line-bot", "litellm", "all"):
        raise HTTPException(status_code=400, detail="target must be line-bot|litellm|all")

    import subprocess as _sp
    import shlex as _shlex
    steps: list = []
    full_stdout: list[str] = []

    def _run(cmd: list[str], cwd: str = "/app") -> dict:
        try:
            r = _sp.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
            out = (r.stdout or "")[-500:]
            err = (r.stderr or "")[-300:]
            full_stdout.extend([f"[STDOUT] {cmd[0]}", out, f"[STDERR]", err])
            return {
                "cmd": " ".join(_shlex.quote(c) for c in cmd),
                "exit_code": r.returncode,
                "stdout_tail": out[-200:],
                "stderr_tail": err[-200:],
            }
        except _sp.TimeoutExpired:
            return {"cmd": " ".join(cmd), "exit_code": -1, "error": "timeout"}
        except FileNotFoundError as e:
            return {"cmd": " ".join(cmd), "exit_code": -1, "error": f"not found: {e}"}
        except Exception as e:
            return {"cmd": " ".join(cmd), "exit_code": -1, "error": str(e)[:200]}

    # docker binary 確認 (= sock mount 経由 host docker を叩く)
    docker_check = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if docker_check.get("exit_code") != 0:
        return {
            "status": "failed",
            "step": "docker_version_check",
            "detail": docker_check,
            "hint": (
                "docker.sock mount or docker CLI 未 install 疑い。"
                "Mac Studio で初回 force-recreate (= compose 設定反映) 必要。"
                "詳細: docs/runbook.md or commit b0f3c5c コメント参照"
            ),
        }
    steps.append({"step": "docker_version", **docker_check})

    # 1. git pull
    git_pull = _run(["git", "pull", "origin", "main"])
    steps.append({"step": "git_pull", **git_pull})

    # 2. docker compose build
    # ★litellm は image-only (build context 無し) → build step を skip。
    # 旧: target=litellm でも `docker compose build litellm` を撃ち、版差で
    #     非 0 終了すると force-recreate 前に abort = model 切替が完了しなかった。
    #     litellm は config bind-mount + 既製 image なので recreate のみで反映される。
    if target == "litellm":
        steps.append({"step": "compose_build", "skipped": "litellm is image-only (no build context)"})
    else:
        if target == "all":
            build = _run(["docker", "compose", "build"])
        else:
            build = _run(["docker", "compose", "build", target])
        steps.append({"step": "compose_build", **build})
        if build.get("exit_code") != 0:
            return {
                "status": "failed",
                "step": "compose_build",
                "steps": steps,
                "detail": "docker build 失敗、code 古いまま稼働継続",
            }

    # 3. force-recreate
    if target == "all":
        recreate = _run(["docker", "compose", "up", "-d", "--force-recreate"])
    else:
        recreate = _run(["docker", "compose", "up", "-d", "--force-recreate", target])
    steps.append({"step": "compose_force_recreate", **recreate})

    return {
        "status": "completed" if recreate.get("exit_code") == 0 else "partial",
        "target": target,
        "steps": steps,
        "next_step": (
            "/api/admin/deploy-status?token=... で container_uptime_hours が低値 (= 0.0-0.1) "
            "なら deploy 反映成功"
        ),
    }


@router.get("/api/admin/deploy-status")
async def admin_deploy_status(request: Request, token: str = Query(...)):
    """auto_deploy 信頼性監視 (★2026-05-24 海山指示「常時監視」核心)。

    container uptime + git HEAD commit + auto_deploy log tail から「deploy stale」判定。
    silent staleness (= auto_deploy build failure 無通知の状態) を即発見可能。

    Returns:
      {
        "container_started_at": "<iso>",        # app.state.startup_at
        "container_uptime_hours": float,        # diff (= 24h+ で stale 疑い)
        "git_head_commit": "<hash>",            # /app/.git/refs/heads/main
        "auto_deploy_log_tail": [...],          # 最新 30 行
        "build_failures_24h": int,              # log 内「docker build failed」grep
        "last_successful_deploy_age_hours": float | null,  # 最新「deploy ok」age
        "alerts": [...]                         # warning / critical 列挙
      }
    """
    check_at_token(token)

    result: dict = {}

    # 1. container uptime (= app startup_at)
    startup_at = getattr(request.app.state, "startup_at", None)
    result["container_started_at"] = startup_at or "(unknown)"
    if startup_at:
        try:
            t0 = datetime.fromisoformat(startup_at)
            uptime_h = (datetime.now(JST) - t0).total_seconds() / 3600
            result["container_uptime_hours"] = round(uptime_h, 2)
        except Exception:
            result["container_uptime_hours"] = -1

    # 2. git HEAD commit (= deploy 済 code の commit hash)
    git_head_path = Path("/app/.git/refs/heads/main")
    if git_head_path.exists():
        try:
            result["git_head_commit"] = git_head_path.read_text(encoding="utf-8").strip()[:12]
        except Exception:
            result["git_head_commit"] = "(read error)"
    else:
        # fallback: /app/.git/HEAD
        head_path = Path("/app/.git/HEAD")
        if head_path.exists():
            try:
                result["git_head_commit"] = head_path.read_text(encoding="utf-8").strip()[:60]
            except Exception:
                result["git_head_commit"] = "(read error)"
        else:
            result["git_head_commit"] = "(.git not mounted)"

    # 3. auto_deploy.log tail (= host side のはず、container 内 mount あれば見える)
    # 推測 path: data/brain/auto_deploy.log (= scrape_cron.sh 等と同 dir convention)
    log_candidates = [
        Path("/app/data/brain/auto_deploy.log"),
        Path("/app/data/brain/logs/auto_deploy.log"),
    ]
    log_tail: list[str] = []
    log_path_found: str | None = None
    for p in log_candidates:
        if p.exists():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                log_tail = lines[-30:]
                log_path_found = str(p)
                break
            except Exception:
                pass
    result["auto_deploy_log_path"] = log_path_found or "(not found)"
    result["auto_deploy_log_tail"] = log_tail

    # 4. log 解析: build failures / last successful deploy age
    build_failures_24h = 0
    last_successful_ts: str | None = None
    if log_tail:
        import re as _re
        # ts pattern: "Mon May 24 09:00:00 JST 2026" or "2026-05-24 09:00:00" 等、緩い match
        cutoff_24h = datetime.now(JST) - timedelta(hours=24)
        for line in log_tail:
            if "docker build failed" in line.lower() or "build 失敗" in line:
                build_failures_24h += 1  # 厳密には 24h 内のみ、簡略化
            if "deploy ok" in line.lower() or "deploy 完了" in line:
                last_successful_ts = line[:25]
    result["build_failures_24h"] = build_failures_24h
    result["last_successful_deploy_line"] = last_successful_ts

    # 5. alerts (= severity 判定)
    alerts: list = []
    uptime_h = result.get("container_uptime_hours", 0)
    if isinstance(uptime_h, (int, float)) and uptime_h > 24:
        alerts.append({
            "type": "container_stale", "severity": "warning",
            "message": f"container 起動から {uptime_h:.1f}h、新 commit が反映されてない可能性 (= auto_deploy 停止 / build failure 疑い)",
        })
    if build_failures_24h > 0:
        alerts.append({
            "type": "build_failures", "severity": "critical",
            "message": f"auto_deploy で {build_failures_24h} 回 docker build failed、bot は古いコードで稼働中",
        })
    if not startup_at:
        alerts.append({
            "type": "startup_unknown", "severity": "info",
            "message": "app.state.startup_at 未設定 (= 旧 deploy?)、新 commit deploy 後に判定可",
        })
    result["alerts"] = alerts
    result["next_step"] = (
        "container_uptime_hours > 24 or build_failures_24h > 0 で要対応。\n"
        "復旧: docker compose build line-bot && docker compose up -d --force-recreate line-bot"
    )

    return result


@router.post("/api/admin/rebuild-data")
async def admin_rebuild_data(
    request: Request,
    token: str = Query(...),
    confirm: str = Query("", description="安全確認: 'yes' 必須"),
):
    """MacBook から sales 集計 wiki を本番で再生成 (★2026-06-05 海山「MacBookで完了まで」)。

    container 内で build_grouped_monthly.py を実行 → prefecture/region/am/sv/type の
    月次・日次集計 + compact を再生成 (data/ は bind-mount なので bot が即読める)。
    swap-fix (8415e6f) もこれで月次データに反映される。
    chromadb 非接触なので CLAUDE.md 1.5 (並行アクセス禁止) に抵触しない。
    """
    check_deploy_token(token)  # ★2026-06-08 評価 0-2: rebuild も deploy 系 token に分離可能
    _audit_privileged(request, "rebuild-data", token)  # ★評価 Security HIGH-3: 監査ログ
    if confirm != "yes":
        raise HTTPException(status_code=400, detail="confirm=yes 必須 (= 誤実行防止)")

    import subprocess as _sp
    import shlex as _shlex

    def _run(cmd: list) -> dict:
        try:
            r = _sp.run(cmd, cwd="/app", capture_output=True, text=True, timeout=300)
            return {
                "cmd": " ".join(_shlex.quote(c) for c in cmd),
                "exit_code": r.returncode,
                "stdout_tail": (r.stdout or "")[-2000:],
                "stderr_tail": (r.stderr or "")[-500:],
            }
        except _sp.TimeoutExpired:
            return {"cmd": " ".join(cmd), "exit_code": -1, "error": "timeout"}
        except Exception as e:  # noqa: BLE001
            return {"cmd": " ".join(cmd), "exit_code": -1, "error": str(e)[:200]}

    res = _run(["python3", "scripts/build_grouped_monthly.py"])
    return {
        "status": "completed" if res.get("exit_code") == 0 else "failed",
        "step": "build_grouped_monthly",
        "result": res,
        "next": "owndays-history-{prefecture,region}-monthly-compact.md 生成 → bot が次の query で読む",
    }


@router.post("/api/eval/run")
async def eval_run(
    request: Request,
    bg_tasks: BackgroundTasks,
    token: str = Query(...),
):
    """eval_set_v1 baseline 計測を BackgroundTask で実行 (★2026-05-24 Tier 2 E)。

    Strategy reviewer 指摘「Plan C v2 で何 % 行ったか の唯一の答え」を満たす絶対基準。
    bot 応答 (smart=Opus 4.8) vs eval_set ideal の cosine + LLM judge (= smart-gpt 系列分離)。

    body: {"version": "v1", "sample": null}
    Returns: {"status":"started","run_id":"...","est_min":"1.5-2.5"} (= 即返却、結果は別 endpoint)

    例:
      curl -X POST "https://brain.example.com/api/eval/run?token=..." \\
        -H "Content-Type: application/json" -d '{"version":"v1"}' | python3 -m json.tool
    """
    check_at_token(token)
    try:
        body = await request.json()
    except Exception:
        body = {}
    version = body.get("version", "v1")
    sample = body.get("sample")

    run_id = f"eval_{version}_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}"

    # ★2026-05-24 bot 内部 in-process 呼出に切替 (= docker exec 失敗回避)
    brain_wiki = getattr(request.app.state, "brain", None)

    async def _run():
        try:
            import sys as _sys
            if "/app/scripts" not in _sys.path:
                _sys.path.insert(0, "/app/scripts")
            from eval_runner import run_all  # type: ignore
            await run_all(version=version, sample=sample, brain_wiki=brain_wiki)
        except Exception as e:
            logger.exception(f"eval_run bg failed: {e}")

    bg_tasks.add_task(_run)
    return {
        "status": "started",
        "run_id": run_id,
        "version": version,
        "sample": sample,
        "est_min": "1.5-2.5",
        "next_step": (
            f"完了後 GET /api/eval/results?version={version} で score 取得、"
            f"data/brain/alignment/eval_results/eval_results_{version}_*.jsonl に詳細 jsonl 保存"
        ),
    }


@router.get("/api/eval/results")
async def eval_results(
    token: str = Query(...),
    version: str = Query("v1"),
    days: int = Query(30, description="過去 N 日 trend"),
):
    """eval_set_v1 baseline 結果 trend を取得 (★2026-05-24)。"""
    check_at_token(token)
    try:
        from pathlib import Path as _P
        summary_log = _P("/app/data/brain/alignment/eval_summary_v1.jsonl")
        if not summary_log.exists():
            return {"version": version, "n_runs": 0, "history": [], "note": "no runs yet"}

        cutoff = datetime.now(JST) - timedelta(days=days)
        history = []
        for line in summary_log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("version") != version:
                    continue
                run_at = rec.get("run_at", "")
                if run_at:
                    try:
                        ts = datetime.fromisoformat(run_at)
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                history.append(rec)
            except Exception:
                continue
        # 最新 N 件
        history = history[-30:]
        latest = history[-1] if history else None
        return {
            "version": version,
            "days": days,
            "n_runs": len(history),
            "latest": latest,
            "history": history,
            "note": (
                "Plan C v2 baseline score。avg_cosine ≥ 0.75 / pass_rate ≥ 0.7 が目標。"
                "by_category で category 別 trend、改善 / 退行 の早期検知。"
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"eval_results error: {e}")


@router.get("/api/monitor/dashboard")
async def monitor_dashboard(
    token: str = Query(...),
    since_a: str = Query("24h", description="期間 a (default 24h)"),
    since_b: str = Query("7d", description="期間 b (default 7d、diff 用)"),
):
    """Plan C v2 Step 6 monitor dashboard (★2026-05-24 Tier 2 D)。

    bot_events.jsonl から category 分布 / length 分布 / fallback 率 / few-shot leak /
    context_prefix_leak を 24h と 7d で集計、diff を返す。MacBook curl 1 発で可。

    Returns:
      {
        "period_a": "24h", "period_a_data": {...},
        "period_b": "7d",  "period_b_data": {...},
        "alerts": [{"type": "context_leak", "count": N, "severity": "critical"}, ...]
      }
    """
    check_at_token(token)
    try:
        import sys as _sys
        if "/app/scripts" not in _sys.path:
            _sys.path.insert(0, "/app/scripts")
        from bot_monitor_daily import aggregate  # type: ignore
        from bot_events import iter_events, parse_since  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"monitor module import failed: {e}")

    events_a = list(iter_events(since_sec=parse_since(since_a)))
    events_b = list(iter_events(since_sec=parse_since(since_b)))
    agg_a = aggregate(events_a)
    agg_b = aggregate(events_b)

    # alerts
    alerts = []
    if agg_a.get("n_context_prefix_leak", 0) > 0:
        alerts.append({
            "type": "context_prefix_leak", "count": agg_a["n_context_prefix_leak"],
            "severity": "critical",
            "message": "fix #1 strip_context_prefix() 漏れ可能性、即調査",
        })
    if agg_a.get("fewshot_leak_count", 0) >= 5:
        alerts.append({
            "type": "fewshot_leak", "count": agg_a["fewshot_leak_count"],
            "severity": "warning",
            "message": "逐語複写検出 5+ 件、few-shot system prompt instruction 違反",
        })
    fallback_rate = agg_a.get("fallback_rate_pct", 0)
    if fallback_rate > 30:
        alerts.append({
            "type": "high_fallback_rate", "value": f"{fallback_rate}%",
            "severity": "warning",
            "message": "retrieval_fallback 30%+ 、threshold or keyword 緩める検討",
        })
    elif fallback_rate < 2 and agg_a["n_turn_started"] > 20:
        alerts.append({
            "type": "low_fallback_rate", "value": f"{fallback_rate}%",
            "severity": "info",
            "message": "retrieval_fallback < 2%、機能してない可能性、threshold 厳しく検討",
        })

    return {
        "period_a": since_a,
        "period_a_data": agg_a,
        "period_b": since_b,
        "period_b_data": agg_b,
        "alerts": alerts,
        "next_step": (
            "本番 query 分布が eval set 30 件 balance と乖離してるか確認。"
            "想定: 挨拶 + 雑談 60-70%、eval は経営判断 7/業務 6 偏重。"
        ),
    }


@router.post("/api/contextual/reindex")
async def contextual_reindex(
    request: Request,
    token: str = Query(...),
):
    """Contextual Retrieval を MacBook curl で実行 (★2026-05-24 海山指示「Mac Studio 手元無い」対応)。

    各 wiki subdir 配下の .md を読み、contextualize_chunks() で Haiku 4.5 経由 context prefix 生成、
    chromadb に re-index。env 変更不要、bot 起動済プロセス内 sequential 実行で chromadb 並行 risk 無。

    Body:
      {
        "subdirs": ["meetings", "decisions"],  # 対象 subdir (= wiki/<subdir>/)
        "max_files": 100                       # 安全上限 (default 100)
      }

    Returns:
      {
        "total_files_scanned": int,
        "total_files_reindexed": int,
        "per_subdir": {
          "meetings": {"n_files": int, "n_chunks_succeed": int, "n_chunks_failed": int, "latency_sec": float},
          "decisions": {...},
        },
        "skipped_short_docs": int,
        "errors": [],
      }

    使用例:
      curl -X POST "https://brain.example.com/api/contextual/reindex?token=..." \\
        -H "Content-Type: application/json" \\
        -d '{"subdirs":["meetings","decisions"]}' | python3 -m json.tool
    """
    check_at_token(token)
    _audit_privileged(request, "contextual-reindex", token)  # ★評価 Security HIGH-3: 監査ログ

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    subdirs = body.get("subdirs", [])
    if not isinstance(subdirs, list) or not subdirs:
        raise HTTPException(status_code=400, detail="subdirs must be non-empty list")
    max_files = int(body.get("max_files", 100))

    # bot プロセス内の BrainIndex を取得
    try:
        from main import app  # type: ignore
        brain_index = getattr(app.state, "index", None) or getattr(app.state, "brain_index", None)
        # brain は brain_wiki.BrainWiki インスタンス、その self.index が BrainIndex
        brain_wiki = getattr(app.state, "brain", None)
        if brain_wiki and not brain_index:
            brain_index = getattr(brain_wiki, "index", None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"BrainIndex 取得失敗: {e}")
    if brain_index is None:
        raise HTTPException(status_code=503, detail="BrainIndex 未初期化")

    import time as _time
    WIKI_DIR = Path("/app/data/brain/wiki")
    per_subdir: dict = {}
    total_scanned = 0
    total_reindexed = 0
    total_short_skip = 0
    errors: list = []

    for sub in subdirs:
        sub = str(sub).strip().strip("/")
        if not sub:
            continue
        target_dir = WIKI_DIR / sub
        if not target_dir.exists():
            per_subdir[sub] = {"error": f"subdir not exists: {target_dir}"}
            continue

        files = sorted(target_dir.glob("*.md"))[:max_files]
        sub_n_files = 0
        sub_succeed = 0
        sub_failed = 0
        sub_short = 0
        t0 = _time.time()

        for f in files:
            total_scanned += 1
            try:
                # force_contextual=True で env 無視、確実に context generate
                await brain_index.index_wiki_file(f, force_contextual=True)
                # ctx_stats は brain_index 内 logger 出力、ここでは file 数のみ集計
                sub_n_files += 1
                total_reindexed += 1
            except Exception as e:
                sub_failed += 1
                errors.append({"file": str(f), "error": f"{type(e).__name__}: {str(e)[:200]}"})

        per_subdir[sub] = {
            "n_files_reindexed": sub_n_files,
            "n_files_failed": sub_failed,
            "latency_sec": round(_time.time() - t0, 1),
        }

    return {
        "total_files_scanned": total_scanned,
        "total_files_reindexed": total_reindexed,
        "per_subdir": per_subdir,
        "errors": errors[:20],
        "n_errors": len(errors),
        "next_step": (
            "完了。bot 応答時の retrieval で contextual prefix 付き chunk が使われる。"
            "GET /api/voice-align/status / 通常 bot 質問 で動作確認可。"
        ),
    }


@router.get("/api/fine-tune/raw-structure-debug")
async def fine_tune_raw_structure_debug(token: str = Query(...)):
    """raw/ 配下の file structure を返す debug endpoint。

    海山「plaud_speaker 0 件 / imported_drive 6 件 想定下回り、真因究明」を受けて新設。
    plaud raw transcript / gdrive 経由 pdf テキスト の path が実在するか確認用。

    Returns:
      {
        "raw/voice/plaud/": {n_files, sample_names[:10]},
        "raw/notes/processed/": {n_files, sample_names[:10]},
        "raw/notes/ gdrive_monday-dash-weekly_*": {n_files, sample_names},
        "raw/notes/ gdrive_focus10*": {n_files, sample_names},
        "raw/notes/ gdrive_plaud-exports_*": {n_files, sample_names},
      }
    """
    check_at_token(token)
    BRAIN = Path("/app/data/brain")

    def _list_dir(p: Path, n: int = 10, pattern: str = "*") -> dict:
        if not p.exists():
            return {"path": str(p), "exists": False, "n_files": 0, "sample": []}
        try:
            files = sorted([
                f for f in p.glob(pattern)
                if f.is_file() and not f.name.startswith(".")
            ], key=lambda x: x.stat().st_mtime, reverse=True)
            sample = [{"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1)} for f in files[:n]]
            return {"path": str(p), "exists": True, "n_files": len(files), "sample": sample}
        except Exception as e:
            return {"path": str(p), "exists": True, "error": str(e)[:200]}

    raw_notes = BRAIN / "raw" / "notes"
    return {
        "raw/voice/plaud/": _list_dir(BRAIN / "raw" / "voice" / "plaud"),
        "raw/notes/processed/": _list_dir(BRAIN / "raw" / "notes" / "processed"),
        "raw/notes/ all": _list_dir(raw_notes, n=20),
        "raw/notes/ gdrive_monday-dash-weekly_*": _list_dir(raw_notes, n=20, pattern="gdrive_monday-dash-weekly_*"),
        "raw/notes/ gdrive_focus10*": _list_dir(raw_notes, n=20, pattern="gdrive_focus10*"),
        "raw/notes/ gdrive_plaud-exports_*": _list_dir(raw_notes, n=20, pattern="gdrive_plaud-exports_*"),
        "raw/notes/processed/ gdrive_monday-dash-weekly_*": _list_dir(raw_notes / "processed", n=20, pattern="gdrive_monday-dash-weekly_*"),
        "raw/notes/processed/ gdrive_plaud-exports_*": _list_dir(raw_notes / "processed", n=20, pattern="gdrive_plaud-exports_*"),
    }


@router.get("/api/fine-tune/sources-debug")
async def fine_tune_sources_debug(token: str = Query(...)):
    """各 source の実 file 状況 + 抽出失敗理由を返す debug endpoint。

    海山「wiki_interview 0 件 / alignment_dir 148 件 期待より少ない、徹底調査」を受けて新設。
    本番の実 file 中身 + 各 record の抽出可否を見て、なぜ件数が少ないかの真因を出す。

    Returns:
      {
        "wiki_interview": {
          "n_files": N, "n_pairs_collected": M,
          "files": [{name, size_kb, sections_found, sample_head_500}],
        },
        "alignment_dir": {
          "n_files": N, "n_pairs_collected": M,
          "per_file": [{name, n_records, n_with_answer, n_substantive, sample_record}],
        }
      }
    """
    check_at_token(token)
    import sys as _sys
    if "/app/scripts" not in _sys.path:
        _sys.path.insert(0, "/app/scripts")
    import build_fine_tune_dataset as bft  # type: ignore
    import re as _re

    debug: dict = {}

    # ─── wiki_interview ─────────────
    iv_dir = bft.INTERVIEW_WIKI_DIR
    iv_info = {"dir": str(iv_dir), "exists": iv_dir.exists()}
    if iv_dir.exists():
        files = sorted(iv_dir.glob("*.md"))
        per_file = []
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
                sections = _re.split(r"\n## ", content)
                per_file.append({
                    "name": f.name,
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "lines": len(content.splitlines()),
                    "sections_found_via_h2_split": len(sections) - 1,
                    "sample_head_500": content[:500],
                })
            except Exception as e:
                per_file.append({"name": f.name, "error": str(e)[:200]})
        iv_info["n_files"] = len(files)
        iv_info["files"] = per_file
        # iter で実際取れる件数
        try:
            iv_info["n_pairs_collected"] = sum(1 for _ in bft.iter_interview_wiki_pairs())
        except Exception as e:
            iv_info["iter_error"] = str(e)[:200]
    debug["wiki_interview"] = iv_info

    # ─── alignment_dir ─────────────
    align_dir = bft.ALIGNMENT_DIR
    al_info = {"dir": str(align_dir), "exists": align_dir.exists()}
    if align_dir.exists():
        files = sorted(align_dir.glob("*.json"))
        per_file = []
        EXCLUDED = {"questions_100.json", "questions_50.json",
                    "interview_coverage.json", "plaud_custom_vocabulary.csv"}
        for f in files:
            if f.name in EXCLUDED:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    per_file.append({"name": f.name, "skip": "not dict"})
                    continue
                answers = data.get("answers") or data.get("entries") or []
                if not isinstance(answers, list):
                    per_file.append({"name": f.name, "skip": "no answers list"})
                    continue
                n_records = len(answers)
                n_with_question = sum(1 for r in answers
                                      if isinstance(r, dict) and (r.get("question") or r.get("q")))
                n_with_answer = sum(1 for r in answers if isinstance(r, dict) and not r.get("skipped") and (
                    (r.get("comment") or "").strip() or
                    (r.get("free_text") or "").strip() or
                    (r.get("choice_text") or "").strip() or
                    (r.get("answer") or "").strip() or
                    (r.get("answer_summary") or "").strip()
                ))
                n_substantive = sum(1 for r in answers
                                    if isinstance(r, dict) and not r.get("skipped") and
                                    bft._is_substantive(
                                        (r.get("comment") or r.get("free_text") or
                                         r.get("choice_text") or r.get("answer") or
                                         r.get("answer_summary") or "").strip(),
                                        min_chars=bft.MIN_SUBSTANTIVE_CHARS_ALIGNMENT
                                    ))
                # sample record
                sample = answers[0] if answers else None
                per_file.append({
                    "name": f.name,
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "n_records": n_records,
                    "n_with_question": n_with_question,
                    "n_with_answer": n_with_answer,
                    "n_substantive": n_substantive,
                    "lost_in_substantive_filter": n_with_answer - n_substantive,
                    "sample_keys": list(sample.keys()) if isinstance(sample, dict) else None,
                    "sample_record": json.dumps(sample, ensure_ascii=False)[:400] if sample else None,
                })
            except Exception as e:
                per_file.append({"name": f.name, "error": str(e)[:200]})
        al_info["n_files"] = len([f for f in files if f.name not in EXCLUDED])
        al_info["per_file"] = per_file
        try:
            al_info["n_pairs_collected"] = sum(1 for _ in bft.iter_alignment_dir_pairs())
        except Exception as e:
            al_info["iter_error"] = str(e)[:200]
    debug["alignment_dir"] = al_info

    return debug


@router.get("/api/fine-tune/dataset-report")
async def fine_tune_dataset_report(
    token: str = Query(...),
    min_quality: int = Query(3, description="採用最低 quality 軸スコア (= 3/4/5)"),
    include_unscored: bool = Query(False, description="quality 未採点 turn も含める"),
):
    """fine-tune dataset 規模 + 品質を MacBook curl で即確認できる endpoint。

    海山「gpt-5.4 tuned 本命、先に data」を受けて、Mac Studio 不要で dataset
    集計を見られる経路。/app/scripts/build_fine_tune_dataset を直 import。

    Returns:
      {
        "summary": {"total": N, "by_source": {...}, "by_quality": {...}},
        "lengths": {"user": {...}, "bot": {...}},
        "verdict": "<件数評価>" (= < 100 不可 / 100-500 最低限 / 500+ OK / ...),
        "report_markdown": "<集計レポート 全文>",
        "next_step": "<件数別の推奨アクション>",
      }
    """
    check_at_token(token)
    if min_quality not in (1, 2, 3, 4, 5):
        raise HTTPException(status_code=400, detail="min_quality must be 1-5")

    # /app/scripts/build_fine_tune_dataset を import
    import sys as _sys
    if "/app/scripts" not in _sys.path:
        _sys.path.insert(0, "/app/scripts")
    try:
        import build_fine_tune_dataset as bft  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"build_fine_tune_dataset import failed: {e}")

    try:
        pairs = bft.collect_pairs(include_unscored, min_quality)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"collect_pairs failed: {e}")

    # source / quality 集計
    from collections import Counter
    by_source: Counter = Counter()
    by_quality: Counter = Counter()
    user_lens, bot_lens = [], []
    for p in pairs:
        src_cat = p["source"].split(":")[0]
        by_source[src_cat] += 1
        q = p.get("min_quality")
        if q is None:
            by_quality["unscored"] += 1
        else:
            by_quality[str(int(q))] += 1
        user_lens.append(len(p["user"]))
        bot_lens.append(len(p["assistant"]))

    def _stats(lst):
        if not lst:
            return {"min": 0, "median": 0, "mean": 0, "max": 0}
        srt = sorted(lst)
        return {
            "min": srt[0],
            "median": srt[len(srt) // 2],
            "mean": int(sum(srt) / len(srt)),
            "max": srt[-1],
        }

    n = len(pairs)

    # 件数評価 (= fine-tune 着手可能性)
    if n < 100:
        verdict = "fine-tune 不可、データ収集継続を推奨"
        next_step = "--include-unscored を試す or response_quality_judge cron が 1-2 週間貯めるのを待つ"
    elif n < 500:
        verdict = "最低限の dataset、initial tuning 可だが continue training 推奨"
        next_step = (
            "進めるなら gpt-4o-mini で initial tuning ($1.20) → A/B で 1 週間観察。"
            "並行で alignment_trial 追加レビュー + Vapi backfill (= 月曜 reminder 項目 8) で dataset 増量"
        )
    elif n < 2000:
        verdict = "✅ 推奨ライン (500-2000 件)、initial を超えた本格運用着手 OK"
        next_step = (
            "gpt-5.4-mini tuning ($1.90) で initial → A/B で control (smart Opus) と比較。"
            "1 週間 response_quality_judge スコアで判定 → 有意プラスなら 100% rollout"
        )
    else:
        verdict = "✅ 強い base、本人像安定ライン"
        next_step = "gpt-5.4 (full) で品質追求 or gpt-5.4-mini でコスパ追求、両方並行 A/B も可"

    report_md = bft.build_report(pairs, include_unscored, min_quality)

    return {
        "filter": {"min_quality": min_quality, "include_unscored": include_unscored},
        "summary": {
            "total": n,
            "by_source": dict(by_source),
            "by_quality": dict(by_quality),
        },
        "lengths": {
            "user_query": _stats(user_lens),
            "bot_response": _stats(bot_lens),
        },
        "verdict": verdict,
        "next_step": next_step,
        "report_markdown": report_md,
    }


@router.post("/api/voice-align/backfill")
async def voice_align_backfill(
    request: Request,
    bg_tasks: BackgroundTasks,
    token: str = Query(...),
):
    """Vapi call ID list を指定して過去 transcript を raw に backfill。

    body: {
      "call_ids": ["019e4ee6-...", ...],   # 最大 20 件
      "auto_extract": true                 # ★2026-05-23 海山指示
                                             saved raw に対して LLM 蒸留も BackgroundTask で走る
                                             → /api/voice-align/status で extracted dir 確認
                                             → LW で /align-voice で蒸留候補レビュー可
    }

    動作:
      - VAPI_PRIVATE_API_KEY を env から読む (= 未設定なら 503)
      - 各 call_id で /scripts/vapi_backfill.fetch_call + extract_transcript + save_raw を呼ぶ
      - 結果配列を即 JSON で返す (= save まで同期、~10 件なら 30s 以内)
      - auto_extract=true なら、saved raw に対して BackgroundTask で
        alignment_interview.extract_session を呼出 (= 各 raw 10-30s × N)
        → response 後に裏で走る、結果は /api/voice-align/status で確認

    Returns:
      {"results": [{"call_id": "...", "saved": true|false, "chars": N, "error": "..."}],
       "auto_extract_started": bool}
    """
    check_at_token(token)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    call_ids = body.get("call_ids", [])
    auto_extract = bool(body.get("auto_extract", False))
    # ★2026-05-23 海山指示 MacBook 完結化:
    # body で vapi_api_key を渡せば env 未設定でも動く (= TLS 経由、log に出さない)
    body_api_key = (body.get("vapi_api_key") or "").strip()
    env_api_key = os.getenv("VAPI_PRIVATE_API_KEY", "")
    if not body_api_key and not env_api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "VAPI_PRIVATE_API_KEY 未設定。以下のどちらか:\n"
                "  (a) Mac Studio で .env に追加 + docker restart\n"
                "  (b) curl body の vapi_api_key field で直渡し (= MacBook 完結)"
            ),
        )

    if not isinstance(call_ids, list) or not call_ids:
        raise HTTPException(status_code=400, detail="call_ids must be non-empty list")
    if len(call_ids) > 20:
        raise HTTPException(status_code=400, detail="too many call_ids (max 20)")

    # /app/scripts/vapi_backfill を import
    import sys as _sys
    if "/app/scripts" not in _sys.path:
        _sys.path.insert(0, "/app/scripts")
    try:
        import vapi_backfill  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"vapi_backfill import failed: {e}")

    results = []
    for cid in call_ids:
        if not isinstance(cid, str):
            results.append({"call_id": str(cid)[:64], "saved": False, "error": "invalid call_id type"})
            continue
        try:
            # body 経由 key を優先 (= MacBook 完結時)、未指定なら env (= Mac Studio .env)
            call = vapi_backfill.fetch_call(cid, api_key=body_api_key)
            transcript = vapi_backfill.extract_transcript(call)
            if not transcript or len(transcript) < vapi_backfill.MIN_TRANSCRIPT_CHARS:
                results.append({
                    "call_id": cid,
                    "saved": False,
                    "chars": len(transcript or ""),
                    "ended_reason": call.get("endedReason", "?"),
                    "duration": call.get("duration"),
                    "error": f"transcript too short (< {vapi_backfill.MIN_TRANSCRIPT_CHARS} chars)",
                })
                continue
            saved_path = vapi_backfill.save_raw(call, transcript, dry_run=False)
            results.append({
                "call_id": cid,
                "saved": saved_path is not None,
                "chars": len(transcript),
                "file": saved_path.name if saved_path else None,
                "skip_reason": "already exists" if saved_path is None else None,
                "ended_reason": call.get("endedReason", "?"),
                "duration": call.get("duration"),
            })
        except Exception as e:
            results.append({
                "call_id": cid,
                "saved": False,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            })

    n_saved = sum(1 for r in results if r.get("saved"))
    n_skipped = sum(1 for r in results if not r.get("saved") and not r.get("error"))
    n_error = sum(1 for r in results if r.get("error"))

    # ★2026-05-23 海山指示 auto_extract: saved raw に対して BackgroundTask で蒸留
    # MacBook curl 1 発で backfill → 蒸留 → LW /align-voice 確認 まで完結。
    saved_files = [r["file"] for r in results if r.get("saved") and r.get("file")]
    if auto_extract and saved_files:
        bg_tasks.add_task(_extract_voice_raws, saved_files)
    auto_extract_started = bool(auto_extract and saved_files)

    return {
        "summary": {
            "total": len(call_ids),
            "saved": n_saved,
            "skipped": n_skipped,
            "error": n_error,
            "auto_extract_started": auto_extract_started,
            "auto_extract_targets": saved_files if auto_extract_started else [],
        },
        "results": results,
        "next_step": (
            "auto_extract=true なら裏で蒸留 走行中、5-10 分後に /api/voice-align/status で extracted dir 確認、"
            "LW で /align-voice で蒸留候補レビュー可"
            if auto_extract_started
            else
            "saved > 0 なら、auto_extract=true で再実行 or Mac Studio で蒸留 loop を回す。"
            "1-shot 蒸留 loop は vapi_backfill.py docstring 参照"
        ),
    }


async def _extract_voice_raws(raw_filenames: list[str]):
    """saved raw に対して alignment_interview.extract_session を逐次呼出。

    BackgroundTask で実行 (= response 後に裏で走る)、所要 5-10 分 × N 件。
    既に extracted dir に json があれば skip (= 重複回避)。
    """
    import logging
    log = logging.getLogger("voice_align_auto_extract")

    import sys as _sys
    if "/app" not in _sys.path:
        _sys.path.insert(0, "/app")
    try:
        import alignment_interview as ai  # type: ignore
    except Exception as e:
        log.exception(f"alignment_interview import failed: {e}")
        return

    raw_dir = ai.RAW_DIR
    extracted_dir = ai.EXTRACTED_DIR
    litellm_url = os.getenv("LITELLM_URL", "http://litellm:4000")
    litellm_key = os.getenv("LITELLM_MASTER_KEY", "")

    http = httpx.AsyncClient(timeout=120.0)
    try:
        for fn in raw_filenames:
            try:
                raw_path = raw_dir / fn
                if not raw_path.exists():
                    log.warning(f"raw not found: {fn}")
                    continue
                # 既蒸留 skip
                if (extracted_dir / (raw_path.stem + ".json")).exists():
                    log.info(f"already extracted, skip: {fn}")
                    continue
                transcript = raw_path.read_text(encoding="utf-8")
                result = await ai.extract_session(
                    transcript, http, litellm_url, litellm_key,
                    raw_filename=fn, model="smart-gpt",   # ★2026-07-04 系列分離 (webhook と同一)
                )
                if result.get("error"):
                    log.warning(f"extract failed ({fn}): {result['error']}")
                else:
                    log.info(f"extracted {fn}: {len(result.get('items', []))} items")
            except Exception as e:
                log.exception(f"extract error ({fn}): {e}")
    finally:
        await http.aclose()


@router.post("/api/voice-align/extract-pending")
async def voice_align_extract_pending(
    bg_tasks: BackgroundTasks,
    token: str = Query(...),
    limit: int = Query(20, description="一度に蒸留する最大 file 数 (= LLM cost 暴走防止)"),
    dry_run: bool = Query(False, description="true なら未蒸留 list だけ返して trigger しない"),
):
    """raw - extracted の差分 (= 未蒸留 raw) を BackgroundTask で再蒸留 trigger。

    ★2026-05-23 海山指示 (Vapi 5 件蒸留漏れ救済、MacBook 完結):
    auto_extract が timeout / fail で落ちた case (= raw あり、extracted 無し) を
    MacBook curl 1 発で再 trigger。

    Returns:
      {
        "pending": [{"raw": "2026-05-23-1648.md", "size_kb": 8.6}, ...],
        "n_pending": N,
        "triggered": [...],   # bg task に渡した file 名 (= dry_run=false の時)
        "dry_run": bool,
        "next_step": "<5-10 分後に /api/voice-align/status で extracted dir 確認>"
      }
    """
    check_at_token(token)

    BRAIN = Path("/app/data/brain")
    raw_dir = BRAIN / "raw" / "alignment_voice"
    extracted_dir = BRAIN / "alignment" / "interview_extracted"

    if not raw_dir.exists():
        return {"pending": [], "n_pending": 0, "note": "raw dir not exists"}

    extracted_stems = set()
    if extracted_dir.exists():
        extracted_stems = {p.stem for p in extracted_dir.glob("*.json")}

    pending = []
    for p in sorted(raw_dir.glob("*.md"), key=lambda q: q.stat().st_mtime, reverse=True):
        if p.stem in extracted_stems:
            continue
        # ★2026-07-04: allowlist 外発信者の隔離 raw (= *-untrusted.md) は蒸留に乗せない
        # (他人の発話が「海山の本人像」として wiki/interview/ に混入するのを防ぐ)
        if p.stem.endswith("-untrusted"):
            continue
        try:
            pending.append({
                "raw": p.name,
                "size_kb": round(p.stat().st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=JST).isoformat(),
            })
        except Exception:
            pass

    pending_to_trigger = pending[:limit]
    triggered_names = [p["raw"] for p in pending_to_trigger]

    if not dry_run and triggered_names:
        bg_tasks.add_task(_extract_voice_raws, triggered_names)

    return {
        "pending": pending,
        "n_pending": len(pending),
        "triggered": triggered_names if not dry_run else [],
        "dry_run": dry_run,
        "limit": limit,
        "next_step": (
            f"BackgroundTask で {len(triggered_names)} 件蒸留中 (= 各 raw ~10-30s)、"
            f"5-10 分後に /api/voice-align/status で extracted dir 確認、"
            f"LW で /align-voice で蒸留候補レビュー可"
            if (not dry_run and triggered_names)
            else
            ("dry_run mode、trigger せず list 返却のみ"
             if dry_run
             else "全 raw 蒸留済、pending 0")
        ),
    }


@router.get("/api/voice-align/status")
async def voice_align_status(token: str = Query(...), limit: int = 10):
    """Voice alignment pipeline (Vapi 電話 → raw → 蒸留 → wiki/interview) の各層 file 状況を返す。

    海山が「蒸留されてない気がする」と疑った時の即時診断用。
    各 dir の最新 N file + 件数を返す → どこで pipeline が詰まったか即判定可。

    Returns:
      {
        "raw":       {"dir": "...", "count": N, "latest": [{name, mtime, size_kb}, ...]},
        "extracted": {"dir": "...", "count": N, "latest": [...]},
        "wiki":      {"dir": "...", "count": N, "files": [...]},
        "coverage":  <coverage JSON> or null,
      }
    """
    check_at_token(token)

    def _list_dir(d: Path, n: int) -> dict:
        if not d.exists():
            return {"dir": str(d), "count": 0, "exists": False, "latest": []}
        files = sorted(
            [p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        latest = []
        for p in files[:n]:
            try:
                latest.append({
                    "name": p.name,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=JST).isoformat(),
                    "size_kb": round(p.stat().st_size / 1024, 1),
                })
            except Exception:
                pass
        return {"dir": str(d), "count": len(files), "exists": True, "latest": latest}

    BRAIN = Path("/app/data/brain")
    result = {
        "raw": _list_dir(BRAIN / "raw" / "alignment_voice", limit),
        "extracted": _list_dir(BRAIN / "alignment" / "interview_extracted", limit),
        "wiki": _list_dir(BRAIN / "wiki" / "interview", 20),  # 9 カテゴリ前提で広め
    }
    # coverage state も読む
    coverage_path = BRAIN / "alignment" / "interview_coverage.json"
    if coverage_path.exists():
        try:
            result["coverage"] = json.loads(coverage_path.read_text(encoding="utf-8"))
        except Exception as e:
            result["coverage_error"] = str(e)
    else:
        result["coverage"] = None

    # 診断: 各層の最新 file 時刻を比較
    diag = []
    raw_latest = result["raw"]["latest"][0]["mtime"] if result["raw"]["latest"] else None
    ext_latest = result["extracted"]["latest"][0]["mtime"] if result["extracted"]["latest"] else None
    if raw_latest and ext_latest:
        if raw_latest > ext_latest:
            diag.append(
                "⚠️ raw (= Vapi 通話 transcript) の方が extracted (= 蒸留結果) より新しい。"
                "蒸留 pipeline が詰まっている可能性。docker logs line-bot --tail 200 | grep voice-align で確認。"
            )
        else:
            diag.append("✓ raw / extracted の時刻関係は正常 (= 蒸留 pipeline 動作中)")
    elif raw_latest and not ext_latest:
        diag.append("🚨 raw あり、extracted ゼロ。LLM 蒸留が 1 度も走っていない (smart route 失敗か、_process_voice_alignment エラー)。")
    elif not raw_latest:
        diag.append("ℹ️ raw ゼロ。直近に Vapi 通話無し or webhook 着信無し。")
    result["diagnosis"] = diag

    return result


@router.get("/api/prompt-diff/latest")
async def prompt_diff_latest(token: str = Query(...), trigger_sha: str = Query("", description="特定 sha の結果を取る、空なら最新")):
    """直近の prompt diff report を返す。

    Returns:
      diff-* file の中身 (= n_compared / n_degraded / n_improved / avg_cosine_delta /
        avg_judge_delta / avg_violations_delta / degraded_questions / improved_questions)
    """
    check_at_token(token)
    if not _REGRESSION_DIR.exists():
        return {"status": "no_data", "note": "regression dir が存在しない (= nightly が走ってない、または PB 起動直後)"}

    # diff-* file をリスト、最新優先
    pattern = f"diff-{trigger_sha[:7]}-*.json" if trigger_sha else "diff-*.json"
    files = sorted(_REGRESSION_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {
            "status": "no_diff_yet",
            "note": (
                "diff report がまだ無い。"
                "POST /api/prompt-diff/run で発火、5-10 分後に再度 GET を試す。"
            ),
        }
    latest = files[0]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")
    return {
        "status": "ok",
        "file": latest.name,
        "mtime": datetime.fromtimestamp(latest.stat().st_mtime, tz=JST).isoformat(),
        "report": data,
    }


# ─── ★2026-05-24 Feature 2/4: Usage Analytics Dashboard ───────────────
# bot_events.jsonl から月間 query 数 / user / channel / failure rate を集計、
# Phase 1 ROI ($1k → $10k/月) progress 可視化。

@router.get("/api/admin/usage")
async def admin_usage(
    token: str = Query(...),
    window: str = Query("30d", description="集計窓 (例: 24h / 7d / 30d / 90d、default 30d)"),
):
    """うみやまAI usage を JSON で返す (= dashboard 用 raw data).

    GET /api/admin/usage?token=<TOKEN>&window=30d

    response: services.usage_analytics.aggregate_usage() の dict.

    使い方:
      curl 'https://brain.example.com/api/admin/usage?token=...&window=7d' | jq .
    """
    check_at_token(token)
    try:
        import sys as _sys
        if "/app/scripts" not in _sys.path:
            _sys.path.insert(0, "/app/scripts")
        from bot_events import parse_since  # type: ignore
        from services.usage_analytics import aggregate_usage
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"usage_analytics import failed: {e}")

    try:
        since_sec = parse_since(window)
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid window: {window}")

    if since_sec <= 0:
        since_sec = 86400 * 30  # default 30 日

    try:
        return aggregate_usage(since_sec=since_sec)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"aggregate failed: {e}")


# ─── ★2026-05-24 統合 Review Dashboard (海山「LINE より操作しやすそう」) ───
# Top page + 各 review queue (= learning / feedback / audit / research / memory / group)
# + POST action endpoints (= accept / reject / noted)

@router.get("/admin/review", response_class=HTMLResponse)
async def admin_review_top(token: str = Query(...), msg: Optional[str] = Query(None)):
    check_at_token(token)
    from services.review_dashboard import render_top_page
    return HTMLResponse(render_top_page(token, flash=msg))


@router.get("/admin/review/learning", response_class=HTMLResponse)
async def admin_review_learning(token: str = Query(...), msg: Optional[str] = Query(None)):
    check_at_token(token)
    from services.review_dashboard import render_learning_page
    return HTMLResponse(render_learning_page(token, flash=msg))


@router.get("/admin/review/feedback", response_class=HTMLResponse)
async def admin_review_feedback(token: str = Query(...), msg: Optional[str] = Query(None)):
    check_at_token(token)
    from services.review_dashboard import render_feedback_page
    return HTMLResponse(render_feedback_page(token, flash=msg))


@router.get("/admin/review/audit", response_class=HTMLResponse)
async def admin_review_audit(token: str = Query(...), msg: Optional[str] = Query(None)):
    check_at_token(token)
    from services.review_dashboard import render_audit_page
    return HTMLResponse(render_audit_page(token, flash=msg))


@router.get("/admin/review/system", response_class=HTMLResponse)
async def admin_review_system(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-25 海山指示: システム修正依頼 (= bug / 機能要望) 一覧 + 直接入力 form."""
    check_at_token(token)
    from services.review_dashboard import render_system_issues_page
    return HTMLResponse(render_system_issues_page(token, flash=msg))


@router.get("/admin/review/voice-align", response_class=HTMLResponse)
async def admin_review_voice_align(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-26 海山指示: Vapi 音声アラインメント 蒸留状況 dashboard."""
    check_at_token(token)
    from services.review_dashboard import render_voice_align_page
    return HTMLResponse(render_voice_align_page(token, flash=msg))


@router.get("/admin/review/quality", response_class=HTMLResponse)
async def admin_review_quality(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-26 海山 C2+C3: 品質 metric 14 日 trend + 直近 alert."""
    check_at_token(token)
    from services.review_dashboard import render_quality_page
    return HTMLResponse(render_quality_page(token, flash=msg))


@router.get("/admin/review/style-reflux", response_class=HTMLResponse)
async def admin_review_style_reflux(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-26 海山 B1+B3: style 逆流 週次レポート 一覧 + 最新内容."""
    check_at_token(token)
    from services.review_dashboard import render_style_reflux_page
    return HTMLResponse(render_style_reflux_page(token, flash=msg))


@router.get("/admin/review/web-clip", response_class=HTMLResponse)
async def admin_review_web_clip(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-26 海山指示: web/他媒体 → wiki 取込 dashboard."""
    check_at_token(token)
    from services.review_dashboard import render_web_clip_page
    return HTMLResponse(render_web_clip_page(token, flash=msg))


@router.get("/admin/review/data-gaps", response_class=HTMLResponse)
async def admin_review_data_gaps(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-26 海山指示: bot「データ無い」 → データ拡充候補 dashboard."""
    check_at_token(token)
    from services.review_dashboard import render_data_gaps_page
    return HTMLResponse(render_data_gaps_page(token, flash=msg))


@router.get("/admin/review/conversation-success", response_class=HTMLResponse)
async def admin_review_conversation_success(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-26 海山指示: 会話継続 = positive signal 一覧 (= success dataset)."""
    check_at_token(token)
    from services.review_dashboard import render_conversation_success_page
    return HTMLResponse(render_conversation_success_page(token, flash=msg))


@router.post("/admin/review/conversation-success/action")
async def admin_review_conversation_success_action(
    request: Request,
    token: str = Query(...),
):
    """★2026-05-26: conversation-success action (= applied / skipped / reviewed)."""
    check_at_token(token)
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up

    form = await request.form()
    action = (form.get("action") or "").strip()
    item_id = (form.get("id") or "").strip()
    note = (form.get("note") or "").strip()

    from services.review_dashboard import handle_conversation_success_action
    ok, message = handle_conversation_success_action(action, item_id, note=note)
    return RedirectResponse(
        url=f"/admin/review/conversation-success?token={token}&msg={_up.quote(message[:200])}",
        status_code=303,
    )


@router.post("/admin/review/data-gaps/action")
async def admin_review_data_gaps_action(
    request: Request,
    token: str = Query(...),
):
    """★2026-05-26: data-gaps page の action (= planned / done / skipped / comment)."""
    check_at_token(token)
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up

    form = await request.form()
    action = (form.get("action") or "").strip()
    item_id = (form.get("id") or "").strip()
    note = (form.get("note") or "").strip()

    from services.review_dashboard import handle_data_gap_action
    ok, message = handle_data_gap_action(action, item_id, note=note)
    return RedirectResponse(
        url=f"/admin/review/data-gaps?token={token}&msg={_up.quote(message[:200])}",
        status_code=303,
    )


@router.post("/admin/review/web-clip/submit")
async def admin_review_web_clip_submit(
    request: Request,
    token: str = Query(...),
):
    """★2026-05-26: web clip form の submit endpoint.

    form fields:
      title (任意), source_url (任意), quote (必須),
      reflection (任意), target_wiki (必須), apply_now (任意 checkbox)
    """
    check_at_token(token)
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up

    form = await request.form()
    title = (form.get("title") or "").strip()
    source_url = (form.get("source_url") or "").strip()
    quote = (form.get("quote") or "").strip()
    reflection = (form.get("reflection") or "").strip()
    target_wiki = (form.get("target_wiki") or "").strip()
    apply_now = bool(form.get("apply_now"))

    if not quote or not target_wiki:
        return RedirectResponse(
            url=f"/admin/review/web-clip?token={token}&msg={_up.quote('引用本文と反映先 wiki が必要です')}",
            status_code=303,
        )

    try:
        from services import web_clips
        clip_id = web_clips.add_clip(
            quote=quote, target_wiki=target_wiki,
            title=title, source_url=source_url, reflection=reflection,
            reviewer="umiyama",
        )
        msg = f"clip 登録 (id: {clip_id})"
        if apply_now:
            result = web_clips.apply_clip(clip_id)
            if result.get("ok"):
                msg = f"clip 登録 + wiki 反映 → {result.get('applied_path')}"
            else:
                msg = f"clip 登録 OK / 反映失敗: {result.get('error', '?')}"
    except ValueError as e:
        return RedirectResponse(
            url=f"/admin/review/web-clip?token={token}&msg={_up.quote(f'入力エラー: {e}')}",
            status_code=303,
        )
    except Exception as e:
        logger.exception(f"web_clip submit failed: {e}")
        return RedirectResponse(
            url=f"/admin/review/web-clip?token={token}&msg={_up.quote(f'登録失敗: {e}')}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/admin/review/web-clip?token={token}&msg={_up.quote(msg[:200])}",
        status_code=303,
    )


@router.post("/admin/review/web-clip/action")
async def admin_review_web_clip_action(
    request: Request,
    token: str = Query(...),
):
    """★2026-05-26: web-clip page の action (= apply / reject / comment)."""
    check_at_token(token)
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up

    form = await request.form()
    action = (form.get("action") or "").strip()
    item_id = (form.get("id") or "").strip()
    note = (form.get("note") or "").strip()

    from services.review_dashboard import handle_web_clip_action
    ok, message = handle_web_clip_action(action, item_id, note=note)
    return RedirectResponse(
        url=f"/admin/review/web-clip?token={token}&msg={_up.quote(message[:200])}",
        status_code=303,
    )


@router.get("/admin/review/voice-align/detail", response_class=HTMLResponse)
async def admin_review_voice_align_detail(
    token: str = Query(...),
    file: str = Query(..., description="extraction JSON filename"),
    msg: Optional[str] = Query(None),
):
    """★2026-05-26: 蒸留案 1 件の per-item 詳細 (= checkbox で selective accept)."""
    check_at_token(token)
    from services.review_dashboard import render_voice_align_detail_page
    return HTMLResponse(render_voice_align_detail_page(token, file, flash=msg))


@router.post("/admin/review/voice-align/action")
async def admin_review_voice_align_action(
    request: Request,
    token: str = Query(...),
):
    """★2026-05-26: voice-align page の action (= accept_all / accept_selected / reject)."""
    check_at_token(token)
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up

    form = await request.form()
    filename = (form.get("file") or "").strip()
    action = (form.get("action") or "").strip()
    indices_raw = form.getlist("indices") if hasattr(form, "getlist") else []
    accepted_indices: list[int] = []
    for s in indices_raw:
        try:
            accepted_indices.append(int(s))
        except Exception:
            pass

    from services.review_dashboard import handle_voice_align_action
    ok, message = handle_voice_align_action(
        filename, action, accepted_indices=accepted_indices,
    )
    return RedirectResponse(
        url=f"/admin/review/voice-align?token={token}&msg={_up.quote(message[:200])}",
        status_code=303,
    )


@router.post("/admin/review/action/submit")
async def admin_review_action_submit(
    request: Request,
    token: str = Query(...),
):
    """★2026-05-25 海山指示: ダッシュボード直接入力 (= 品質 / システム 2 mode) の submit endpoint.

    form fields:
      mode    = "quality" | "system"
      content = 改善 / 修正 の本文 (必須)
      detail  = 補足 (品質 = wiki patch 案 / システム = 期待動作、任意)

    routing:
      quality → clone_learning.add_manual_entry → /admin/review/learning へ redirect
      system  → services.system_issues.add_entry → /admin/review/system へ redirect
    """
    check_at_token(token)
    from fastapi.responses import RedirectResponse
    import urllib.parse as _up

    form = await request.form()
    mode = (form.get("mode") or "quality").strip()
    content = (form.get("content") or "").strip()
    detail = (form.get("detail") or "").strip()

    if not content:
        # content 必須、エラーメッセージで redirect back
        redirect = "/admin/review/learning" if mode == "quality" else "/admin/review/system"
        return RedirectResponse(
            url=f"{redirect}?token={token}&msg={_up.quote('内容は必須です')}",
            status_code=303,
        )

    try:
        if mode == "system":
            from services import system_issues
            item_id = system_issues.add_entry(content, expected=detail, reviewer="umiyama")
            msg = f"システム修正依頼 登録 (id: {item_id})"
            redirect = "/admin/review/system"
        else:  # quality default
            import clone_learning
            item_id = clone_learning.add_manual_entry(content, proposed_wiki_patch=detail, reviewer="umiyama")
            msg = f"品質改善 登録 (id: {item_id})"
            redirect = "/admin/review/learning"
    except Exception as e:
        logger.exception(f"admin_review_action_submit failed: {e}")
        redirect = "/admin/review/learning" if mode == "quality" else "/admin/review/system"
        return RedirectResponse(
            url=f"{redirect}?token={token}&msg={_up.quote(f'登録失敗: {e}')}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"{redirect}?token={token}&msg={_up.quote(msg[:200])}",
        status_code=303,
    )


@router.get("/admin/review/research", response_class=HTMLResponse)
async def admin_review_research(token: str = Query(...), msg: Optional[str] = Query(None)):
    check_at_token(token)
    from services.review_dashboard import render_research_page
    return HTMLResponse(render_research_page(token, flash=msg))


@router.get("/admin/review/memory", response_class=HTMLResponse)
async def admin_review_memory(token: str = Query(...), msg: Optional[str] = Query(None)):
    check_at_token(token)
    from services.review_dashboard import render_memory_page
    return HTMLResponse(render_memory_page(token, flash=msg))


@router.get("/admin/review/memory/{user_id}", response_class=HTMLResponse)
async def admin_review_memory_detail(
    user_id: str,
    token: str = Query(...),
    msg: Optional[str] = Query(None),
):
    """個別 user の memory 詳細 + 会話履歴 (= プライバシー mask 適用)."""
    check_at_token(token)
    from services.review_dashboard import render_memory_detail_page
    return HTMLResponse(render_memory_detail_page(user_id, token, flash=msg))


@router.get("/admin/review/group", response_class=HTMLResponse)
async def admin_review_group(token: str = Query(...), msg: Optional[str] = Query(None)):
    check_at_token(token)
    from services.review_dashboard import render_group_page
    return HTMLResponse(render_group_page(token, flash=msg))


@router.post("/admin/review/{queue}/action")
async def admin_review_action(
    queue: str,
    request: Request,
    token: str = Query(...),
):
    """review queue の accept/reject/noted/comment action.

    ★2026-05-24 v2: queue='audit' を追加、note (= 任意 comment) field 受付.
    POST 完了後、当該 page に redirect + flash message.

    queue:
      - learning / feedback / research: 既存 status update + 任意 comment
      - audit: source='needs_attention' or 'unrated' 別に handle_audit_action へ
    """
    check_at_token(token)
    # ★2026-05-25: system queue 追加 (= システム修正依頼 backlog)
    if queue not in ("learning", "feedback", "research", "audit", "system"):
        raise HTTPException(status_code=404, detail=f"unknown queue: {queue}")

    from fastapi.responses import RedirectResponse
    form = await request.form()
    action = form.get("action", "")
    note = form.get("note", "")

    if queue == "audit":
        # audit: source 別に handle
        from services.review_dashboard import handle_audit_action
        source = form.get("source", "")
        item_id = form.get("id", "")
        index = form.get("index", "")
        ok, message = handle_audit_action(action, source, item_id=item_id, index=index, note=note)
    else:
        item_id = form.get("id", "")
        if not item_id or not action:
            raise HTTPException(status_code=400, detail="id and action are required")
        # ★2026-05-25: learning queue は proposed_wiki_patch を編集可能化、
        # patch (= textarea 現値) + patch_original (= hidden 旧値) を form から受け取り、
        # 差分があれば handle_action 経由で clone_learning.update_patch 呼出。
        patch = form.get("patch", "")
        patch_original = form.get("patch_original", "")
        from services.review_dashboard import handle_action
        ok, message = handle_action(queue, action, item_id, note=note,
                                    patch=patch, patch_original=patch_original)

    # URL encode message (= 安全策、改行 / 特殊文字)
    import urllib.parse as _up
    redirect_path = f"/admin/review/{queue}" if queue != "audit" else "/admin/review/audit"
    return RedirectResponse(
        url=f"{redirect_path}?token={token}&msg={_up.quote(message[:200])}",
        status_code=303,
    )


@router.get("/admin/usage", response_class=HTMLResponse)
async def admin_usage_html(
    token: str = Query(...),
    window: str = Query("30d"),
):
    """うみやまAI usage を HTML dashboard で返す (= 海山 browser 用).

    GET /admin/usage?token=<TOKEN>&window=30d

    ブラウザで開くと Phase 1 ROI progress / channel split / top users / components / daily trend が見える。
    """
    check_at_token(token)
    try:
        import sys as _sys
        if "/app/scripts" not in _sys.path:
            _sys.path.insert(0, "/app/scripts")
        from bot_events import parse_since  # type: ignore
        from services.usage_analytics import aggregate_usage, render_dashboard_html
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"usage_analytics import failed: {e}")

    try:
        since_sec = parse_since(window) or 86400 * 30
    except Exception:
        since_sec = 86400 * 30

    try:
        data = aggregate_usage(since_sec=since_sec)
        return HTMLResponse(render_dashboard_html(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"dashboard render failed: {e}")


# ─── ★2026-05-29 海山指示「ダッシュボードに API 料金 + 課金状況の track」 ───
# 課金状況 (= 今日 spend / 日次 budget) は LiteLLM /spend 実値、per-turn 内訳は events 推定。

async def _fetch_litellm_spend() -> dict:
    """LiteLLM /spend を取得し budget gauge 用 dict を返す.

    返り値 (= render_cost_page の litellm_status):
      {used_usd, budget_usd, usage_pct}  又は  {error: str}

    external_credit_watchdog.check_litellm() と同 logic (= /spend → /spend/logs[today]
    fallback)。watchdog を import せず inline するのは line_push 依存を route に持ち込まない為。
    """
    if not LITELLM_KEY:
        return {"error": "LITELLM_MASTER_KEY 未設定"}
    cap = float(os.getenv("LITELLM_MAX_BUDGET", "50") or 50)
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    used_usd = 0.0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{LITELLM_URL}/spend",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            )
            if r.status_code == 200:
                data = r.json()
                used_usd = float(data.get("spend") or data.get("total_spend") or 0)
            else:
                r2 = await client.get(
                    f"{LITELLM_URL}/spend/logs",
                    headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                    params={"limit": 1000},
                )
                if r2.status_code == 200:
                    logs = r2.json()
                    if isinstance(logs, list):
                        for log in logs:
                            if str(log.get("startTime", "")).startswith(today_str):
                                used_usd += float(log.get("spend") or 0)
                else:
                    return {"error": f"/spend={r.status_code} /spend/logs={r2.status_code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    pct = round(used_usd / cap * 100, 1) if cap > 0 else 0.0
    return {"used_usd": round(used_usd, 2), "budget_usd": cap, "usage_pct": pct}


@router.get("/admin/review/cost", response_class=HTMLResponse)
async def admin_review_cost(token: str = Query(...), msg: Optional[str] = Query(None)):
    """★2026-05-29 海山指示: API 料金 + 課金状況の track ページ.

    GET /admin/review/cost?token=<TOKEN>
    - 課金状況: LiteLLM /spend 実値で今日 spend / 日次 budget gauge
    - per-turn 内訳: events.jsonl usage から provider/model/component 別 USD 推定
    - 調査メモ: コスト要因 top 5 + 対策案 (= 海山「調査して」への回答)
    """
    check_at_token(token)
    import sys as _sys
    if "/app/scripts" not in _sys.path:
        _sys.path.insert(0, "/app/scripts")
    try:
        from services.review_dashboard import render_cost_page
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"review_dashboard import failed: {e}")
    litellm_status = await _fetch_litellm_spend()
    return HTMLResponse(render_cost_page(token, flash=msg, litellm_status=litellm_status))


@router.get("/api/admin/cost")
async def admin_cost_json(token: str = Query(...)):
    """API 料金集計を JSON で返す (= dashboard 用 raw data / 外部監視).

    GET /api/admin/cost?token=<TOKEN>

    response: services.usage_analytics.aggregate_cost() の dict
              + "litellm_spend" (= LiteLLM /spend 実値、取れれば)
    """
    check_at_token(token)
    import sys as _sys
    if "/app/scripts" not in _sys.path:
        _sys.path.insert(0, "/app/scripts")
    try:
        from services.usage_analytics import aggregate_cost
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"usage_analytics import failed: {e}")
    try:
        data = aggregate_cost(since_sec=86400 * 14)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"aggregate_cost failed: {e}")
    data["litellm_spend"] = await _fetch_litellm_spend()
    return data

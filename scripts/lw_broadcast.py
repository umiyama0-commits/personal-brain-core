#!/usr/bin/env python3
"""lw_broadcast.py — うみやまAI 登録ユーザへの一斉 DM (★2026-07-13 海山「アナウンスは直接送れる?」).

対象 = clone_history に会話履歴がある全 user (= bot を登録して一度でも接触した社員)。
LINE WORKS Bot API の 1:1 送信 (lineworks_bot.send_text = 1900 字自動分割) を throttle 付きで反復。

**安全設計 (一斉送信 = 外向き mass op)**:
- default は dry-run: 対象者数 + 宛先一覧 + 文面プレビューだけ出して送信しない
- 実送信は `--send` 必須 + 対話確認 ("yes" 入力)。cron 登録禁止 (単発運用のみ)
- 文面はファイル渡し (`--message-file`) = 送信内容が git/ファイルで監査可能
- `--exclude` で user_id 除外、`--limit N` でテスト送信 (例: 海山自身に 1 通)
- 送信結果は JSONL (data/brain/broadcast_log/) に記録、失敗は最後にまとめて表示

使い方 (Mac Studio):
  cd ~/brain-agent && set -a && . ./.env && set +a
  python3 scripts/lw_broadcast.py --message-file /tmp/announce.txt              # dry-run
  python3 scripts/lw_broadcast.py --message-file /tmp/announce.txt --limit 1 --send   # 自分でテスト
  python3 scripts/lw_broadcast.py --message-file /tmp/announce.txt --send      # 全員 (要 yes 入力)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# host 実行 (container 外) では clone_history の default /app/data/brain が存在しない →
# repo 配下の data/brain へ自動フォールバック (env 明示があればそれを尊重)
import os  # noqa: E402
if not os.getenv("BRAIN_ROOT") and not Path("/app/data/brain").exists():
    os.environ["BRAIN_ROOT"] = str(ROOT / "data" / "brain")

THROTTLE_SEC = 0.6  # LW API rate limit への礼儀 (114 人 ≈ 70 秒)
LOG_DIR = ROOT / "data" / "brain" / "broadcast_log"


def _targets(exclude: set[str], limit: int | None) -> list[dict]:
    import clone_history
    users = clone_history.list_users()
    out = [u for u in users if u["user_id"] not in exclude]
    out.sort(key=lambda u: -(u.get("last_updated") or 0))  # アクティブ順 (テスト送信が自分に当たりやすい)
    return out[:limit] if limit else out


async def _send_all(targets: list[dict], text: str) -> list[dict]:
    import httpx

    import lineworks_bot
    results = []
    async with httpx.AsyncClient(timeout=30) as http:
        for i, u in enumerate(targets, 1):
            uid = u["user_id"]
            try:
                await lineworks_bot.send_text(http, uid, text)
                ok = True
                err = ""
            except Exception as e:
                ok = False
                err = str(e)[:200]
            results.append({"user_id": uid, "display": u.get("display"),
                            "ok": ok, "error": err})
            print(f"  [{i}/{len(targets)}] {'✓' if ok else '✗'} "
                  f"{u.get('display') or uid[:8]}{(' — ' + err) if err else ''}")
            await asyncio.sleep(THROTTLE_SEC)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="うみやまAI 登録ユーザへの一斉 DM")
    ap.add_argument("--message-file", required=True, help="送信文面のテキストファイル")
    ap.add_argument("--send", action="store_true", help="実送信 (無指定は dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="先頭 N 人だけ (テスト送信用)")
    ap.add_argument("--exclude", default="", help="除外 user_id (comma 区切り)")
    args = ap.parse_args()

    text = Path(args.message_file).read_text(encoding="utf-8").strip()
    if not text:
        print("文面が空。中止。")
        return 1
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    targets = _targets(exclude, args.limit)

    print(f"対象: {len(targets)} 人 (clone_history 登録者{' 先頭 ' + str(args.limit) + ' 人' if args.limit else ''})")
    print(f"文面 ({len(text)} 字):\n{'─' * 40}\n{text}\n{'─' * 40}")
    for u in targets[:10]:
        print(f"  - {u.get('display') or '?'} ({u['user_id'][:8]}…, {u['message_count']} msg)")
    if len(targets) > 10:
        print(f"  … 他 {len(targets) - 10} 人")

    if not args.send:
        print("\n[dry-run] 送信していない。実送信は --send を付ける (対話確認あり)。")
        return 0

    ans = input(f"\n{len(targets)} 人に実送信する。よろしいか? (yes と入力): ").strip()
    if ans != "yes":
        print("中止。")
        return 1

    results = asyncio.run(_send_all(targets, text))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"broadcast_{stamp}.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    ng = [r for r in results if not r["ok"]]
    print(f"\n完了: 成功 {len(results) - len(ng)} / 失敗 {len(ng)} → log: {log_path}")
    for r in ng:
        print(f"  ✗ {r.get('display') or r['user_id'][:8]}: {r['error']}")
    return 0 if not ng else 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""scripts/ingest_audit.py — 取り込み後の日次監査 (★2026-08-09)

## なぜ要るか

2026-08-06〜09 に **同じ型の事故が 4 回** 出た。いずれも「入れる時は通り、入った後に気づいた」:

1. クローンの誤答を auto_improve が翌朝 wiki 化し能力否定を固定
2. 自動生成 wiki の public 昇格で未発表の人事・報酬制度が社員 114 人に流れかけた
3. 調査エージェントが §1.9 の文書を読んだ
4. 公式議事録 101 件が wiki と raw の両方に索引され retrieval 予算を二重消費

入口の門 (gdrive denylist / visibility 判定) は増やしたが、**入った後に効く歯止め**が無かった。

## この監査が **できないこと** (先に書く)

- **機密の網羅検知はできない**。(A) は §1.9 が定義した語彙を拾うだけで、
  婉曲表現・英語・数字だけの表は抜ける。**「0 件 = 安全」ではない**
- 内容の真偽は判定できない (事故 #1 は別 guard に依存)
- chroma 索引の実体は見ない (markdown のみ)

初版はこの限界を弁えず「あらゆる機密を検知する」つもりで書き、§1.15 で 4 件の critical を
受けた: ①正規表現が数字を要求せずカンマ 1 個で成立 ②差し止めた 1 ファイルの字面を写した
過学習で、同じ内容が既に public な別ファイルは 0 件検知 ③その状態で threshold=1 + 日次 cron =
毎日誤報 ④「public 汚染 0」という緑が **誤った安心の生成器** になる。
本版は (A) を「§1.9 の語彙を拾う補助」に格下げし、判定を **増分** にした。

## 検知するもの

(A) §1.9 語彙 — public な wiki に、相談/面談/通報/評価/健康/懲戒 の語 (日英)
    ただし **規程 (制度文書) は対象外**。「相談の記録」と「相談制度の規程」は別物で、
    後者は社員が読むべきもの (実測: 内部通報規程 / ハラスメント防止規程 / 懲戒手続細則 は
    public が正しい。private にすべきだったのは相談の記録 1 件だけだった)
(B) 二重索引 — 同一 source が wiki と raw/notes の両方
(C) 意図されていない可視性 — frontmatter が **丸ごと無い** wiki
    (clone_visibility 未指定は既定 private = 正常なので対象外。初版はここを混ぜて
     1,253 件の誤警報を出した)

## 方針

- **propose-only**。wiki は一切書き換えない (書くのは検知済みを覚える state file のみ)。
  事故 #1 は「AI が自分で直した」ことが原因なので、直すのは人間の側に残す
- **read-only**。chroma には触らない (§1.5)。wiki の markdown だけを読む

## 判定

**前回からの増分**で鳴らす。絶対件数だと、海山が受容した既知の 1 件で毎日鳴り続ける
(§1.18 が bug と定める「毎日同文 alert」)。state に見たものを覚え、**新しく増えた時だけ** loud。

usage:
  python3 scripts/ingest_audit.py            # 表示のみ (state 更新なし)
  python3 scripts/ingest_audit.py --push     # 増分を LINE 通知 + state 更新 (§1.18)
  python3 scripts/ingest_audit.py --reset    # state をいまの状態で初期化 (既知を全部受容)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
WIKI = BASE_DIR / "data" / "brain" / "wiki"
RAW_NOTES = BASE_DIR / "data" / "brain" / "raw" / "notes"
STATE = BASE_DIR / "data" / "brain" / "clone_improve" / ".ingest_audit_seen.json"

# (A) §1.9 が定義したカテゴリの語彙 (日英)。私の推測ではなく **ポリシーの写し**。
# (k) 相談/面談/通報 / (a) 人事評価 / (e) 健康 / (f) 懲戒。
POLICY_MARKERS = re.compile(
    r"grievance|harassment|whistleblow|counsel(l)?ing|consultation\s*log|"
    r"performance\s*review|disciplinary|medical\s*record|"
    r"相談記録|相談対応|相談ログ|相談履歴|面談記録|面談ログ|個別面談|1 ?on ?1|"
    r"内部通報|ハラスメント|メンタルヘルス|人事評価|人事考課|懲戒|健康診断",
    re.IGNORECASE,
)
# 未発表の人事・報酬制度 (事故 #2)。語幹 + 近接で書く — 完全一致の複合語だと
# 要約で原語が崩れた瞬間に抜ける (実測: 差し止めた 1 件にしか当たらなかった)。
TOPIC_MARKERS = re.compile(
    r"等級[^\n]{0,10}(統一|改定|再設計|見直し)|"
    r"インセンティブ[^\n]{0,10}(再設計|スキーム|制度の見直し)|"
    r"昇給(率|方針|案)|賞与原資|マイル(単価|レート)[^\n]{0,10}(見直|改定)|マイレージ料率|"
    r"卸売価格[^\n]{0,20}(変更|改定)|仕入単価[^\n]{0,12}[0-9][0-9,]*|"
    r"家賃[^\n]{0,12}[0-9][0-9,]*万円|賃料[^\n]{0,12}[0-9][0-9,]*万円",
    re.IGNORECASE,
)
# 規程 (制度文書) は上記の語を正当に語る。ここを鳴らすと規程が全部鳴る。
POLICY_DOC_PREFIXES = ("imported_drive/regulations/",)

_FM_VIS = re.compile(r"^clone_visibility:\s*(\S+)", re.M)
_FM_SRCID = re.compile(r"^source_id:\s*(\S+)", re.M)


def _visibility(text: str) -> str:
    if not text.lstrip().startswith("---"):
        return "(frontmatter なし)"
    m = _FM_VIS.search(text[:1200])
    # clone_visibility 未指定は **既定 private で正常** (この Brain の設計)
    return m.group(1) if m else "default-private"


def audit() -> dict:
    leaks: list[tuple[str, str]] = []
    no_fm: list[str] = []
    wiki_stems: dict[str, str] = {}

    for p in sorted(WIKI.rglob("*.md")):
        rel = str(p.relative_to(WIKI))
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        vis = _visibility(t)
        if vis == "(frontmatter なし)":
            no_fm.append(rel)
        elif vis == "public" and not rel.startswith(POLICY_DOC_PREFIXES):
            # 名前と見出しを先に見る (§1.9 の文書を不必要に開かない)
            head = rel + "\n" + "\n".join(
                l for l in t.splitlines()[:40] if l.startswith("#"))
            m = POLICY_MARKERS.search(head) or TOPIC_MARKERS.search(t)
            if m:
                leaks.append((rel, m.group(0)[:40]))
        if _FM_SRCID.search(t[:1200]):
            wiki_stems[Path(rel).stem] = rel

    # (B) 同一 source が raw/notes にも居る = 二重索引。
    #     部分文字列一致だと短い名前が誤マッチするので **完全一致** で突き合わせる。
    dup: list[tuple[str, str]] = []
    if RAW_NOTES.exists():
        raw_by_tail = defaultdict(list)
        for p in RAW_NOTES.glob("gdrive_*"):
            # gdrive_<label>_<name> → <name> 部分を取る (label は wiki 側の dir 名)
            parts = p.stem.split("_", 2)
            if len(parts) == 3:
                raw_by_tail[parts[2]].append(p.name)
        for stem, wrel in wiki_stems.items():
            if stem in raw_by_tail:
                dup.append((wrel, raw_by_tail[stem][0]))

    return {"policy_leak": leaks, "no_frontmatter": no_fm, "double_indexed": dup}


def _load_seen() -> set:
    try:
        return set(tuple(x) for x in json.loads(STATE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(items: set) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(sorted(list(items)), ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  state 保存失敗 (次回また同じものを新規扱いする): {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="増分を LINE 通知 (§1.18)")
    ap.add_argument("--reset", action="store_true", help="いまの状態を既知として記録")
    args = ap.parse_args()

    r = audit()
    now = set()
    for rel, hit in r["policy_leak"]:
        now.add(("leak", rel, hit))
    for rel in r["no_frontmatter"]:
        now.add(("nofm", rel, ""))
    for wrel, raw in r["double_indexed"]:
        now.add(("dup", wrel, raw))

    seen = _load_seen()
    new = now - seen

    print(f"§1.9 語彙 (public): {len(r['policy_leak'])} / frontmatter なし: "
          f"{len(r['no_frontmatter'])} / 二重索引: {len(r['double_indexed'])}")
    print(f"  うち **前回からの新規**: {len(new)} 件")
    print("  ※ (A) は §1.9 の語彙を拾う補助であって網羅検知ではない。0 件は安全の証明ではない")
    for kind, a, b in sorted(new)[:12]:
        print(f"  [新規/{kind}] {a}  {b}")

    if args.reset:
        _save_seen(now)
        print(f"  state を初期化: {len(now)} 件を既知として記録")
        return 0

    if args.push:
        sys.path.insert(0, str(BASE_DIR / "scripts"))
        from clone_improve_lib import loud_fail
        new_leak = [x for x in new if x[0] == "leak"]
        # public への流出は増分ゼロを常態にしたいので即時 (threshold=1)。
        # 既知は state にあるので鳴らない = 毎日同文 alert にならない
        loud_fail("ingest_audit_leak", not new_leak,
                  "public な wiki に §1.9 語彙が **新たに** 出た: "
                  + ", ".join(f"{a} ({b})" for _, a, b in new_leak[:3]),
                  threshold=1, cooldown_h=24)
        others = [x for x in new if x[0] != "leak"]
        loud_fail("ingest_audit_hygiene", not others,
                  f"新規 {len(others)} 件 (frontmatter なし / 二重索引)。"
                  "前者は可視性が意図されていない、後者は retrieval 予算の二重消費",
                  threshold=3, cooldown_h=72)
        _save_seen(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
build_store_master.py — store_master_<date>.json を権威ソースから生成。

build_grouped_monthly.py が要求する
  data/brain/raw/notes/store_master_<date>.json = [{code, prefecture, am, sv}, ...]
を、唯一の権威マッピング
  data/brain/wiki/knowledge/owndays-area-managers.md
  (= Google Drive「担当店舗表」由来、confidence:high)
からパースして生成する。

★捏造しない: 推測で店舗→県/AM を埋めない。area-managers.md に
明示されている値のみ採用。不明 (- / 空 / 未割当) は null。
これにより build_grouped_monthly の prefecture/AM/SV 集計が
本物のマッピングに基づく (Personal Brain の信頼性原則)。

owndays-area-managers.md 構造:
  ### AM: <am名> (N 店、SV M 名)
  #### SV: <sv名> (K 店)
  | 店舗コード | 店舗名 | エリア | 都道府県 | J | 直営/FC | 店長 |
  | 2239 | まるひろ南浦和SC |  | 埼玉 | - | 直営 | 0 |

実行: python3 scripts/build_store_master.py
出力: data/brain/raw/notes/store_master_YYYY-MM-DD.json
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "brain" / "wiki" / "knowledge" / "owndays-area-managers.md"
OUT_DIR = ROOT / "data" / "brain" / "raw" / "notes"

# "未割当" / "-" / "" は不明扱い (null)
_UNKNOWN = {"", "-", "未割当", "(未割当)", "ー", "—"}


def _clean(v: str):
    v = (v or "").strip()
    return None if v in _UNKNOWN else v


def _heading_name(line: str, prefix: str):
    """'### AM: 中田 (12 店、SV 2 名)' → '中田' / '(未割当)' → None"""
    m = re.match(rf"^#+\s*{prefix}\s*[:：]\s*(.+?)\s*(?:\([^)]*\))?\s*$", line)
    if not m:
        return None
    return _clean(m.group(1))


def main():
    if not SRC.exists():
        print(f"ERROR: 権威ソース無し: {SRC}", file=sys.stderr)
        sys.exit(1)
    text = SRC.read_text(encoding="utf-8")

    cur_am = None
    cur_sv = None
    by_code: dict[int, dict] = {}
    # 行テーブル: | code | name | area | prefecture | J | 直営/FC | 店長 |
    # ★2026-06-05 海山指示: 6 列目 直営/FC も capture (= 都道府県/AM/SV × 直営/FC
    # クロス集計を build_grouped_monthly で可能化)。
    row_re = re.compile(
        r"^\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|"
        r"\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|"
    )
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("### AM"):
            cur_am = _heading_name(s, "AM")
            cur_sv = None
            continue
        if s.startswith("#### SV"):
            cur_sv = _heading_name(s, "SV")
            continue
        m = row_re.match(s)
        if not m:
            continue
        # ヘッダ行 (| 店舗コード | ...) は group(1) が数字でないので弾かれる
        try:
            code = int(m.group(1))
        except ValueError:
            continue
        pref = _clean(m.group(4))
        # ★ 直営/FC を正規化 (= 「直営」「FC」 のみ有効、それ以外は null)
        raw_type = _clean(m.group(6))
        store_type = raw_type if raw_type in ("直営", "FC") else None
        # 同一 code が複数 SV 配下に出る事は通常無いが、後勝ち回避で初出優先
        if code not in by_code:
            by_code[code] = {
                "code": code,
                "name": _clean(m.group(2)),
                "prefecture": pref,
                "am": cur_am,
                "sv": cur_sv,
                "type": store_type,
                # ★2026-06-10: エリア (関東A 等) も capture (= am-sv-summary 生成用)
                "area": _clean(m.group(3)),
            }

    master = sorted(by_code.values(), key=lambda x: x["code"])
    if not master:
        print("ERROR: 1 件もパースできなかった (フォーマット変化?)", file=sys.stderr)
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"store_master_{date.today().isoformat()}.json"
    out.write_text(
        json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    n_pref = sum(1 for m in master if m["prefecture"])
    n_am = sum(1 for m in master if m["am"])
    n_sv = sum(1 for m in master if m["sv"])
    n_chokuei = sum(1 for m in master if m.get("type") == "直営")
    n_fc = sum(1 for m in master if m.get("type") == "FC")
    print(f"  wrote {out.name}: {len(master)} 店")
    print(f"  都道府県あり {n_pref} / AM あり {n_am} / SV あり {n_sv}")
    print(f"  直営 {n_chokuei} / FC {n_fc} / 区分不明 {len(master) - n_chokuei - n_fc}")
    print(f"  (不明は null、捏造なし。ソース: owndays-area-managers.md)")

    write_am_sv_summary(master)


def write_am_sv_summary(master: list[dict]) -> None:
    """★2026-06-10 (rg-09 根治): core 常駐の owndays-am-sv-summary.md を同一権威ソースから機械生成。

    旧 summary は手動 snapshot (5/14) で store-master (5/19) と店数が乖離 (田口 46 vs 47 等) し、
    bot が古い数字を答えて correctness eval の捏造判定を受けていた。さらに LLM compile が
    別 frontmatter ごと第 2 文書を append する破損もあった。本 writer で都度全文上書き
    (= 手編集・LLM append は次回 build で消える、単一ソース原則)。"""
    from collections import defaultdict

    by_am: dict[str, list[dict]] = defaultdict(list)
    unassigned = []
    for s in master:
        (by_am[s["am"]] if s["am"] else unassigned).append(s)  # type: ignore[index]

    today_s = date.today().isoformat()
    lines = [
        "---",
        f"updated: {today_s}",
        "confidence: high",
        "tags: [OWNDAYS, 組織, 営業本部, AM, SV, サマリ]",
        "sources: [knowledge/owndays-area-managers.md]",
        "clone_visibility: public",
        "---",
        "# OWNDAYS 営業本部 AM/SV サマリ",
        "",
        f"> ★ {today_s} 機械生成 (build_store_master.py、ソース: owndays-area-managers.md = 担当店舗表)。",
        "> 手編集禁止 (次回 build で上書き)。店舗別の詳細一覧は knowledge/owndays-area-managers.md に。",
        "",
    ]
    for am, stores in sorted(by_am.items(), key=lambda kv: -len(kv[1])):
        svs: dict[str, list[dict]] = defaultdict(list)
        for s in stores:
            svs[s["sv"] or "(SV未割当)"].append(s)
        areas = sorted({s["area"] for s in stores if s.get("area")})
        n_sv_named = sum(1 for k in svs if k != "(SV未割当)")
        lines.append(f"## AM: {am} ({len(stores)} 店、SV {n_sv_named} 名、エリア: {', '.join(areas) or '-'})")
        for sv, ss in sorted(svs.items(), key=lambda kv: -len(kv[1])):
            sv_areas = sorted({s["area"] for s in ss if s.get("area")})
            lines.append(f"  - SV: {sv} ({len(ss)} 店、{', '.join(sv_areas) or '-'})")
        lines.append("")
    n_total = len(master)
    n_am_assigned = sum(len(v) for v in by_am.values())
    all_svs = {s["sv"] for s in master if s["sv"]}
    lines += [
        "## 集計",
        f"- AM (エリアマネージャー): {len(by_am)} 名",
        f"- SV (スーパーバイザー): {len(all_svs)} 名",
        f"- AM 配下店舗: {n_am_assigned} 店 / 未割当・特殊: {n_total - n_am_assigned} 店 (計 {n_total} 店)",
        "",
        "## 関連",
        "- [[knowledge/owndays-area-managers]] — 詳細 (店舗別)",
        "- [[knowledge/owndays-store-master]] — 担当店舗表 (集計サマリ込み)",
        "- [[knowledge/owndays-organization]] — 取締役 + 執行役員",
        "",
    ]
    out = ROOT / "data" / "brain" / "wiki" / "knowledge" / "owndays-am-sv-summary.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {out.name}: AM {len(by_am)} / SV {len(all_svs)} / AM配下 {n_am_assigned} 店 (単一ソース同期)")


if __name__ == "__main__":
    main()

"""brain_wiki_helpers/kpi_table.py — kpi-dash 営業数値テーブルの**ヘッダー駆動**パーサ。

★2026-07-27: 固定列数 (18) 前提のパーサが silent に全行を捨てていた事故の根治。

背景: kpi-dash の Table 1 は 2 段ヘッダーで、列数が view によって違う。
  週次 (4-a): 名称 + [前週|今週|予算比|前年比|既存前年比|…]  → data row 19 cells
  当月 (4-b): 名称 + [当月|予算比|前年比|既存前年比|…]        → data row 14 cells
旧実装は `len(cells) != 18: continue` の**固定列数**チェックで、**両方とも 18 でないため
全行が捨てられていた**。結果、列取り違えを防ぐための簡約表と「全国まとめ」canonical が
生成されず (無警告)、bot は dense な生テーブルだけを見る状態が続いていた。

本 helper は**サブヘッダーのラベル位置から列を決める**ので、前週/当月列の増減や
列の並び替えに強い (再発しない)。数値は解釈せず文字列のまま返す (決定論・捏造ゼロ)。
"""
from __future__ import annotations

# サブヘッダーを特定する必須ラベル (この 3 つが揃う行を列定義とみなす)
_REQUIRED = ("予算比", "前年比", "既存前年比")
# 「現在値」列のラベル候補 (予算比の直前に来る)
_CURRENT_LABELS = ("今週", "当月", "今月")


def _split(line: str) -> list[str]:
    return [c.strip() for c in line.split("|")]


def find_header(table_block: str) -> list[str] | None:
    """列ラベル行 (サブヘッダー) を返す。見つからなければ None。"""
    for line in (table_block or "").split("\n"):
        if "|" not in line:
            continue
        cells = _split(line)
        if all(any(r == c for c in cells) for r in _REQUIRED):
            return cells
    return None


def parse_rows(table_block: str) -> list[dict]:
    """営業数値 Table 1 を [{name, sales, budget_ratio, yoy, existing_yoy}, ...] に。

    ★列は**ラベル位置**から決める (固定 index を使わない)。`既存前年比` は売上グループの
    最初の出現を採る (客数/ATV グループにも同名ラベルがあるため)。
    値が `—` / 空 の cell は None。
    """
    header = find_header(table_block)
    if not header:
        return []
    try:
        i_budget = header.index("予算比")
    except ValueError:
        return []
    # 売上グループ: 予算比 の直後が 前年比、その次が 既存前年比、直前が現在値 (今週/当月)
    i_yoy = i_budget + 1 if header[i_budget + 1:i_budget + 2] == ["前年比"] else None
    i_exist = i_budget + 2 if header[i_budget + 2:i_budget + 3] == ["既存前年比"] else None
    i_cur = i_budget - 1 if i_budget >= 1 else None
    if i_cur is not None and header[i_cur] not in _CURRENT_LABELS:
        # 「前週|今週|予算比」のように現在値ラベルが期待どおりでない時も、直前列を現在値とみなす
        # (ラベル表記ゆれへの耐性。位置関係は kpi-dash で一貫している)
        pass

    def _val(cells: list[str], idx: int | None) -> str | None:
        if idx is None:
            return None
        j = idx + 1                     # data row は先頭に 名称 が付くので +1
        if j >= len(cells):
            return None
        v = cells[j].strip()
        return None if not v or v == "—" else v

    out: list[dict] = []
    for line in (table_block or "").split("\n"):
        if "|" not in line:
            continue
        cells = _split(line)
        # data row 条件: 先頭が非空・数値でない (= エリア名) かつ 列数がヘッダー+1 に一致
        if len(cells) != len(header) + 1:
            continue
        name = cells[0]
        if not name or name in ("名称",) or any(name == h for h in header):
            continue
        out.append({
            "name": name,
            "sales": _val(cells, i_cur),
            "budget_ratio": _val(cells, i_budget),
            "yoy": _val(cells, i_yoy),
            "existing_yoy": _val(cells, i_exist),
        })
    return out


def find_row(rows: list[dict], name: str = "全国") -> dict | None:
    for r in rows:
        if r.get("name") == name:
            return r
    return None

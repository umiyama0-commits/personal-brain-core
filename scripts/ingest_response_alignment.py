#!/usr/bin/env python3
"""
ingest_response_alignment.py — 海山本人が記入した response_alignment フォーム JSON を
data/brain/wiki/style/response-bank.md に反映する。

実行:
    python3 scripts/ingest_response_alignment.py <path-to-json>
    例: python3 scripts/ingest_response_alignment.py ~/Downloads/umiyama_response_alignment_2026-05-XX.json

冪等: 同じスクリプト再実行で response-bank.md を上書き再生成。
"""
import json
import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
WIKI_OUT = ROOT / "data" / "brain" / "wiki" / "style" / "response-bank.md"

# build_response_alignment_form.py と同じ問のリスト
# (フォーム生成と ingest で問の定義を一致させるため、importable にしてもよいが
#  独立性のためコピー)
QUESTIONS = [
    {"id": "q1", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "最近、休日は何してる?"},
    {"id": "q2", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "最近ハマってる食べ物は?"},
    {"id": "q3", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "普段、移動中は何してる?"},
    {"id": "q4", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "最近観た映画 / 読んだ本で印象的だったのは?"},
    {"id": "q5", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "ストレス溜まった時どうしてる?"},
    {"id": "q6", "category": "B", "category_label": "経営判断", "scale": "M-L", "q": "30 億の投資判断、何を見て決める?"},
    {"id": "q7", "category": "B", "category_label": "経営判断", "scale": "M", "q": "不採算店舗の閉店、いつ判断する?"},
    {"id": "q8", "category": "B", "category_label": "経営判断", "scale": "M-L", "q": "海外進出、どの国を優先する?"},
    {"id": "q9", "category": "B", "category_label": "経営判断", "scale": "M", "q": "競合と価格戦争になりそうな時の判断軸は?"},
    {"id": "q10", "category": "B", "category_label": "経営判断", "scale": "M-L", "q": "M&A の話が来た時、最初に何を見る?"},
    {"id": "q11", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "30 代でキャリア迷ってる後輩にどう声かける?"},
    {"id": "q12", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "会社辞めるか迷ってる社員に何て言う?"},
    {"id": "q13", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "起業したいって相談されたら何を確認する?"},
    {"id": "q14", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "燃え尽きそうな部下にどう接する?"},
    {"id": "q15", "category": "C", "category_label": "キャリア相談", "scale": "S-M", "q": "やる気が出ない時、自分はどうしてる?"},
    {"id": "q16", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "フランス留学で一番影響を受けたことは?"},
    {"id": "q17", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "20 代で何を考えてた?"},
    {"id": "q18", "category": "D", "category_label": "自伝・回想", "scale": "M-L", "q": "OWNDAYS が一番しんどかった時期はどう乗り越えた?"},
    {"id": "q19", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "東南アジアでの生活で身についたことは?"},
    {"id": "q20", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "起業を決めた瞬間のことは覚えてる?"},
    {"id": "q21", "category": "E", "category_label": "価値観", "scale": "M", "q": "「Take Bold Risks」 を社員に説明するとしたら?"},
    {"id": "q22", "category": "E", "category_label": "価値観", "scale": "M", "q": "「正しさより美しさ」 ってどういう意味?"},
    {"id": "q23", "category": "E", "category_label": "価値観", "scale": "M", "q": "成功とは何か、いま改めて聞かれたら?"},
    {"id": "q24", "category": "E", "category_label": "価値観", "scale": "M", "q": "リーダーシップで一番大事だと思うことは?"},
    {"id": "q25", "category": "E", "category_label": "価値観", "scale": "S-M", "q": "死ぬまでにやりたいことは?"},
    {"id": "q26", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "新店候補地を見る時、何を最初にチェックする?"},
    {"id": "q27", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "不調店舗の見立て、どう立てる?"},
    {"id": "q28", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "店長候補を選ぶ時、何を見る?"},
    {"id": "q29", "category": "F", "category_label": "業務オペ", "scale": "S-M", "q": "クレーム対応で大事にしてることは?"},
    {"id": "q30", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "売上が落ちた時、まずどこを疑う?"},
]


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/ingest_response_alignment.py <path-to-json>")
        sys.exit(1)
    json_path = Path(sys.argv[1])
    if not json_path.exists():
        print(f"Not found: {json_path}")
        sys.exit(1)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    answers = data.get("answers", data)

    today = datetime.now().strftime("%Y-%m-%d")
    filled = sum(1 for q in QUESTIONS if (answers.get(q["id"]) or "").strip())

    md = [
        "---",
        "type: response_bank",
        "id: response-bank-30q",
        "category: language_style",
        f"last_updated: {today}",
        f"last_validated: {today}",
        f"last_observed: {today}",
        f"last_reviewed: {today}",
        "confidence: high",
        "evidence:",
        f"  - {json_path.name}",
        "counter_evidence: []",
        "clone_visibility: public",
        "exit_visibility: public",
        "tags: [スタイル, 応答, 参考回答例, アライメント]",
        "related_wiki: [[style/style-response-examples]], [[style/style-response-mirroring]], [[style/style-personal-flavor]]",
        "---",
        "# 海山本人 — 想定 30 質問への参考回答例",
        "",
        f"> {filled} / {len(QUESTIONS)} 問 記入済 (最終更新: {today})。",
        "> ★ 重要: これは **参考回答例** (★海山本人記入)。",
        "> bot 応答時に **逐語的に真似しない**、**回答の長さ・構造も厳密に合わせない**。",
        "> 抽出すべきは: **トーン / 温度 / 語尾 / 軸 / 思考の運び方 / コーティングの入れ方**。",
        "> 質問が違えば回答も違う、文脈に応じて自分で組み立てる。",
        "",
        "---",
        "",
    ]

    # カテゴリ別に整理
    cats = {}
    for q in QUESTIONS:
        cats.setdefault(q["category"], {"label": q["category_label"], "qs": []})["qs"].append(q)

    for cat_key, info in cats.items():
        md.append(f"## {cat_key}. {info['label']}")
        md.append("")
        for q in info["qs"]:
            ans = (answers.get(q["id"]) or "").strip()
            md.append(f"### {q['id'].upper()}: {q['q']}")
            md.append(f"**想定スケール**: {q['scale']}")
            md.append("")
            if ans:
                # ブロック引用 (海山の生の言葉を保持、Claude が要約しない)
                for line in ans.split("\n"):
                    md.append(f"> {line}" if line else ">")
            else:
                md.append("> *(未記入)*")
            md.append("")
        md.append("---")
        md.append("")

    md.append("## 使い方 (bot 側)")
    md.append("")
    md.append("- 類似質問が来た時、該当 Q の **トーン / 温度 / 軸** を参照する")
    md.append("- 参考回答例を **逐語的に再現しない**、length も厳密に合わせない")
    md.append("- 抽出すべきは:")
    md.append("  - 語尾の癖 (〜かな / 〜よ / 〜だね / 知らんけど / じゃない / ですよ)")
    md.append("  - コーティング型 (「— ま、〜けどな」「知らんけど」「人間だもの」等)")
    md.append("  - 思考の運び方 (前提→軸→具体、または直球→補足→開き)")
    md.append("  - 何を出さないか (自慢・教科書臭・カッコ良さの押し付け)")
    md.append("  - 質問が曖昧な時の聞き返し (Q27 参照)")
    md.append("- 文脈・相手・タイミングが違えば回答も違う、これは type ではなく source")
    md.append("")
    md.append("## 関連")
    md.append("- [[style/style-response-examples]] — 応答の良い形 (個別実例)")
    md.append("- [[style/style-response-mirroring]] — 応答スケール")
    md.append("- [[style/style-personal-flavor]] — 血肉化された記憶")
    md.append("- [[style/style-soften-cliche]] — クサさをコーティング")
    md.append("- [[style/style-no-bragging]] — 自慢しない")
    md.append("- [[style/style-nihilistic-humor]] — ニヒルなユーモア")

    WIKI_OUT.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote: {WIKI_OUT}")
    print(f"  {filled} / {len(QUESTIONS)} 問 反映")


if __name__ == "__main__":
    main()

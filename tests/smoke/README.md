# Smoke tests

基本機能の単体テスト。外部サービス (LLM API / Chroma / Docker) は使わず、
全テストが `tmp_path` で隔離された環境で実行される。

## 実行

```bash
# 全 smoke test
python3 -m pytest tests/smoke/ -v

# 個別ファイル
python3 -m pytest tests/smoke/test_clone_history.py -v

# marker で絞る
python3 -m pytest -m smoke -v
```

## カバー範囲

~92 test ファイル / 830+ tests (2026-07-03 時点)。この規模で per-file 表は維持できないため、
一覧は `python3 -m pytest tests/smoke/ --collect-only -q` で確認する (= 真実源)。

## 設計原則

1. **本物の data/brain には絶対触れない** — `tmp_path` で隔離
2. **LLM / Chroma は呼ばない** — mock or import skip
3. **fixture は conftest.py に集約** — `brain_root`, `sample_clone_history`, `sample_alignment_extracted`
4. **環境変数で path 切替** — `BRAIN_ROOT` / `BRAIN_APP_ROOT` を monkeypatch で設定

## 追加方針

新規 module を追加した時:
1. `tests/smoke/test_<module>.py` を作る
2. 入出力契約のみ test、内部実装は test しない
3. 既存 fixture を使う

新規 wiki カテゴリを追加した時:
1. `test_wiki_structure.py` の `test_essential_*_files_exist` に追加
2. 「必須」と「optional」を区別する

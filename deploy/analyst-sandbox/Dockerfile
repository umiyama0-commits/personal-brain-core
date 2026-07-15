# Analyst sandbox image — データアナリスト・エージェント用の locked-down Python 実行環境。
# ★2026-06-20 ADR docs/decisions/2026-06-20-data-analyst-agent.md §4。
#
# このイメージ自体は「許可された分析ライブラリだけ入った素の python」。
# 実セキュリティは run 時のフラグが担う(scripts/analyst/sandbox.py が付与):
#   --network none / --read-only / --cap-drop ALL / no-new-privileges / --user 65534 /
#   --memory --cpus --pids-limit / 実行時間上限 / 秘密(.env)非注入 / data は read-only mount。
#
# build (prod/Mac Studio):
#   docker build -t analyst-sandbox:latest deploy/analyst-sandbox
FROM python:3.12-slim

# 日本語チャート用 CJK フォント(matplotlib の豆腐化を防ぐ)。
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 分析に必要な allowlist ライブラリのみ。ネットワーク系・OS 操作系は入れない。
RUN pip install --no-cache-dir \
        pandas \
        numpy \
        matplotlib \
        scikit-learn \
        scipy \
        statsmodels \
    && rm -rf /root/.cache /tmp/*

# 秘密・認証情報は image に焼かない。実行は run 時 --user 65534:65534(nobody)で非root強制。
WORKDIR /work
# entrypoint は置かない。sandbox.py が `python -I -B /work/analysis.py` を timeout 付きで起動する。

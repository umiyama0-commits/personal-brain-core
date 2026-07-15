FROM python:3.12-slim
WORKDIR /app

# ★2026-05-26 21:55 緊急: build-essential 追加 (= 「docker build 失敗」 alert 対応)
# 理由: python:3.12-slim は gcc/g++ 無し → wheel 不在 package (= chromadb 1.5.9 等が
#   突然 cp39-abi3 only に変わった場合) で source build path に落ちて build 失敗。
# 対策: build-essential を install しておけば、wheel 無い package も source build で
#   救える (= image size +~150MB の trade-off、復旧優先)。
# 副次効果: 将来 別 dependency (lxml / cryptography / pymupdf 等) が wheel 不在に
#   なっても build 通る safety net。
# ★2026-05-27 海山指示「動画対応 進めて」: ffmpeg 追加 (= 動画 frame 抽出用、
#   content_extractor.extract_video_frames で使用)。+~50MB.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

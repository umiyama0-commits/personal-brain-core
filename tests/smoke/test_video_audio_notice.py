"""smoke test: video / audio input の受信認識 + 明示返信 (★2026-05-27 海山指示)

旧: parse_webhook で video / audio は drop (= return None) → bot 無反応
新: 受信認識 + handler で 「現状未対応、静止画なら可能 / text 化を」 と返信
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


# ─── L1: parse_webhook が video / audio を accept ─────
@pytest.mark.smoke
def test_parse_webhook_accepts_video_and_audio():
    src = (REPO / "lineworks_bot.py").read_text(encoding="utf-8")
    idx = src.find("def parse_webhook")
    assert idx > 0
    body = src[idx : idx + 4000]
    # video / audio 受信認識 (= drop しない)
    assert 'ctype in ("video", "audio")' in body
    # type を そのまま伝達
    assert '"type": ctype' in body
    # file_id 抽出 (= image 経路 と同じ)
    assert "fileId" in body or "contentId" in body


# ─── L2: webhook handler type filter に video / audio 含む ─────
@pytest.mark.smoke
def test_webhook_handler_accepts_video_audio_type():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find('parsed["type"] not in')
    assert idx > 0
    window = src[idx : idx + 200]
    assert '"video"' in window
    assert '"audio"' in window


# ─── L3: _handle_lineworks_message が video → 本実装 / audio → notice ─────
@pytest.mark.smoke
def test_video_dispatch_to_handler():
    """video は _handle_lineworks_video に dispatch (= 本実装、ffmpeg + Vision)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # _handle_lineworks_message 内 (= LINE Works 経路) の video 分岐を探す
    handler_idx = src.find("async def _handle_lineworks_message")
    assert handler_idx > 0
    handler_end = src.find("\nasync def ", handler_idx + 1)
    handler_body = src[handler_idx:handler_end if handler_end > 0 else handler_idx + 10000]
    # video → _handle_lineworks_video
    assert 'if msg_type == "video":' in handler_body
    assert "_handle_lineworks_video" in handler_body


@pytest.mark.smoke
def test_audio_replies_with_notice():
    """audio は依然 notice (= 「次は音声会話」 で別 commit)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    handler_idx = src.find("async def _handle_lineworks_message")
    assert handler_idx > 0
    handler_end = src.find("\nasync def ", handler_idx + 1)
    handler_body = src[handler_idx:handler_end if handler_end > 0 else handler_idx + 10000]
    # audio 分岐
    assert 'if msg_type == "audio":' in handler_body
    # audio 用 notice
    assert "🎤" in handler_body or "音声" in handler_body
    assert "書き起こし" in handler_body or "text" in handler_body


@pytest.mark.smoke
def test_handler_does_not_break_existing_paths():
    """既存 path (= text / file / image / postback) は変更なし."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # text / file / image / postback の handler は維持
    assert "msg_type == \"postback\"" in src
    assert 'msg_type in ("file", "image"):' in src


# ─── L4: 動画本実装 (★2026-05-27 海山指示「動画対応 進めて」) ─────
@pytest.mark.smoke
def test_extract_video_functions_exist():
    """content_extractor に extract_video_frames + extract_video_text 定義."""
    src = (REPO / "content_extractor.py").read_text(encoding="utf-8")
    assert "async def extract_video_frames" in src
    assert "async def extract_video_text" in src
    # ffmpeg 経由 + every_n_seconds parameter
    assert "ffmpeg" in src
    assert "every_n_seconds" in src
    # cost cap (= max_frames で frame 数 上限)
    assert "max_frames" in src


@pytest.mark.smoke
def test_extract_video_text_uses_fast_gpt_for_economy():
    """default model は経済的な fast-gpt (= gpt-5.4-mini) 又は fast (= GPT-4o)."""
    src = (REPO / "content_extractor.py").read_text(encoding="utf-8")
    idx = src.find("async def extract_video_text")
    assert idx > 0
    body = src[idx : idx + 3000]
    # default model が経済的 (= fast / fast-gpt のいずれか)
    assert 'model: str = "fast-gpt"' in body or 'model: str = "fast"' in body


@pytest.mark.smoke
def test_dockerfile_installs_ffmpeg():
    """Dockerfile に ffmpeg install (= 動画 frame 抽出用)."""
    src = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "ffmpeg" in src
    # build-essential + ffmpeg の order が apt-get install line に
    assert "apt-get install" in src


@pytest.mark.smoke
def test_handle_lineworks_video_handler_exists():
    """_handle_lineworks_video handler 定義 (= LINE Works video 専用)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "async def _handle_lineworks_video" in src
    # _handle_lineworks_message 内の dispatch path (= 別 LINE 公式 path の "video" を除く)
    handler_idx = src.find("async def _handle_lineworks_message")
    assert handler_idx > 0
    handler_end = src.find("\nasync def ", handler_idx + 1)
    handler_body = src[handler_idx:handler_end if handler_end > 0 else handler_idx + 10000]
    assert "_handle_lineworks_video" in handler_body


@pytest.mark.smoke
def test_video_handler_uses_extract_video_text_and_clone_respond():
    """_handle_lineworks_video が extract_video_text → attached_content → clone_respond_public."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_lineworks_video")
    assert idx > 0
    # 関数 body 終端 (= 次の async def まで) を取得
    end_idx = src.find("\nasync def ", idx + 1)
    body = src[idx : end_idx if end_idx > 0 else idx + 10000]
    # extract_video_text 呼出
    assert "extract_video_text" in body
    # clone_respond_public 経由 (= _safe_clone_respond)
    assert "_safe_clone_respond" in body
    # attached_content として渡す
    assert "attached_content=extracted" in body
    # サイズ上限 100MB
    assert "CLONE_VIDEO_MAX_BYTES" in body or "100 * 1024 * 1024" in body


# ─── L5: polish 3 点 (★2026-05-27 海山指示) ─────
@pytest.mark.smoke
def test_sniff_extension_supports_video_formats():
    """sniff_extension が .mp4 / .mov / .avi / .mkv / .webm / .m4v / .flv に対応."""
    src = (REPO / "content_extractor.py").read_text(encoding="utf-8")
    idx = src.find("def sniff_extension")
    assert idx > 0
    body = src[idx : idx + 3000]
    # 動画 magic byte 判定
    assert "ftyp" in body  # MP4 / MOV / M4V 共通
    assert '".mov"' in body
    assert '".mp4"' in body
    assert '".m4v"' in body
    # AVI / MKV
    assert "b\"AVI \"" in body or "b'AVI '" in body or '"AVI"' in body or 'AVI' in body
    assert '".avi"' in body
    assert '".mkv"' in body  # WebM も同 magic


@pytest.mark.smoke
def test_video_sampling_dynamic_by_duration():
    """_calc_video_sampling が duration で interval / max_frames を動的に."""
    src = (REPO / "content_extractor.py").read_text(encoding="utf-8")
    idx = src.find("def _calc_video_sampling")
    assert idx > 0
    body = src[idx : idx + 1500]
    # 短い動画 = 1 秒毎
    assert "duration_sec <= 10" in body
    assert "return 1, 10" in body
    # 中 = 3 秒毎 (= 既存 default)
    assert "duration_sec <= 30" in body
    assert "return 3, 10" in body
    # 長 (30-60s) = 5 秒毎
    assert "duration_sec <= 60" in body
    assert "return 5, 12" in body
    # 60s 超 = 動画全体 sampling
    assert "duration_sec / 12" in body or "duration / 12" in body


@pytest.mark.smoke
def test_extract_video_text_auto_dynamic_when_none():
    """extract_video_text に every_n_seconds=None / max_frames=None で auto 動的設定."""
    src = (REPO / "content_extractor.py").read_text(encoding="utf-8")
    idx = src.find("async def extract_video_text")
    assert idx > 0
    body = src[idx : idx + 3500]
    # default が Optional[int] = None
    assert "every_n_seconds: Optional[int] = None" in body
    assert "max_frames: Optional[int] = None" in body
    # ffprobe で duration probe
    assert "_probe_video_duration" in body
    # _calc_video_sampling で動的決定
    assert "_calc_video_sampling" in body


@pytest.mark.smoke
def test_extract_video_thumbnail_function_exists():
    """extract_video_thumbnail (= 最初の 1 frame quick preview) が存在."""
    src = (REPO / "content_extractor.py").read_text(encoding="utf-8")
    assert "async def extract_video_thumbnail" in src
    # ffmpeg で -ss 0.5 -vframes 1 (= 0.5 秒目の 1 frame、黒 frame 回避)
    idx = src.find("async def extract_video_thumbnail")
    body = src[idx : idx + 1500]
    assert "-ss" in body
    assert "-vframes" in body


@pytest.mark.smoke
def test_video_handler_prefetches_thumbnail_with_async_task():
    """_handle_lineworks_video が thumbnail prefetch を asyncio.create_task で並行送信."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_lineworks_video")
    assert idx > 0
    body = src[idx : idx + 6000]
    # _video_thumbnail_prefetch helper 呼出
    assert "_video_thumbnail_prefetch" in body
    # asyncio.create_task で並行
    assert "asyncio.create_task(_video_thumbnail_prefetch" in body
    # 本解析と並行 (= ack 後 / extract_video_text 前)
    assert "extract_video_text" in body


@pytest.mark.smoke
def test_video_handler_uses_sniff_extension_for_format():
    """_handle_lineworks_video が sniff_extension で正しい extension audit 保存."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_lineworks_video")
    assert idx > 0
    body = src[idx : idx + 6000]
    assert "sniff_extension" in body
    # default mp4 fallback
    assert '.mp4' in body


# ─── L6: 音声 input scaffold (★2026-05-27 海山指示「音声会話対応 = Mac Studio 作業」) ─────
@pytest.mark.smoke
def test_extract_audio_text_function_exists():
    """content_extractor.extract_audio_text (= Whisper integration) が存在."""
    src = (REPO / "content_extractor.py").read_text(encoding="utf-8")
    assert "async def extract_audio_text" in src
    idx = src.find("async def extract_audio_text")
    body = src[idx : idx + 2500]
    # /v1/audio/transcriptions endpoint
    assert "/v1/audio/transcriptions" in body
    # multipart file upload
    assert "files = {" in body or "files=" in body
    # ja 日本語 default
    assert 'language: str = "ja"' in body or '"ja"' in body
    # whisper model
    assert 'model: str = "whisper"' in body or '"whisper"' in body


@pytest.mark.smoke
def test_litellm_config_has_whisper_model():
    """litellm_config.yaml に whisper model 登録 (= openai/whisper-1)."""
    src = (REPO / "litellm_config.yaml").read_text(encoding="utf-8")
    assert "- model_name: whisper" in src
    assert "openai/whisper-1" in src


@pytest.mark.smoke
def test_audio_handler_gated_by_env_flag_default_off():
    """音声 handler は AUDIO_TRANSCRIBE_ENABLED env flag で gate、default OFF (= safer rollout)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    handler_idx = src.find("async def _handle_lineworks_message")
    assert handler_idx > 0
    handler_end = src.find("\nasync def ", handler_idx + 1)
    handler_body = src[handler_idx:handler_end if handler_end > 0 else handler_idx + 10000]
    # flag check + default "0" (OFF)
    assert 'os.getenv("AUDIO_TRANSCRIBE_ENABLED"' in handler_body
    assert 'getenv("AUDIO_TRANSCRIBE_ENABLED", "0")' in handler_body
    # flag ON → _handle_lineworks_audio
    assert "_handle_lineworks_audio" in handler_body
    # flag OFF → notice 文言維持
    assert "Mac Studio 設定待ち" in handler_body or "書き起こし機能" in handler_body


@pytest.mark.smoke
def test_handle_lineworks_audio_handler_exists():
    """_handle_lineworks_audio handler 定義 + extract_audio_text + clone_respond 経路."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "async def _handle_lineworks_audio" in src
    idx = src.find("async def _handle_lineworks_audio")
    end_idx = src.find("\nasync def ", idx + 1)
    body = src[idx : end_idx if end_idx > 0 else idx + 10000]
    # extract_audio_text 呼出
    assert "extract_audio_text" in body
    # clone_respond 経由
    assert "_safe_clone_respond" in body
    # 25MB Whisper 仕様上限
    assert "CLONE_AUDIO_MAX_BYTES" in body or "25 * 1024 * 1024" in body
    # 書き起こし結果を user に expose (= 誤認識早期検知)
    assert "📝 書き起こし" in body or "書き起こし" in body

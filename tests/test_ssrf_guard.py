"""content_extractor の SSRF ガード test。

★2026-06-08 システム評価 Security HIGH-2: extract_url は LINE Works 社員の任意 URL を
fetch する (非管理者から到達可能)。private/loopback/link-local/metadata を拒否する
_validate_public_url が、内部サービス (litellm/redis) や cloud metadata への SSRF を
止めることを保証する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from content_extractor import _validate_public_url  # noqa: E402


def test_blocks_loopback_litellm():
    ok, reason = _validate_public_url("http://127.0.0.1:4000/v1/models")
    assert ok is False
    assert "127.0.0.1" in reason


def test_blocks_localhost():
    ok, _ = _validate_public_url("http://localhost:6379")
    assert ok is False


def test_blocks_cloud_metadata_ip():
    ok, _ = _validate_public_url("http://169.254.169.254/latest/meta-data/")
    assert ok is False


def test_blocks_metadata_hostname():
    ok, reason = _validate_public_url("http://metadata.google.internal/computeMetadata/")
    assert ok is False
    assert "hostname" in reason


def test_blocks_private_ranges():
    for url in ("http://10.0.0.5", "http://192.168.1.1", "http://172.16.0.1"):
        ok, _ = _validate_public_url(url)
        assert ok is False, url


def test_blocks_non_http_schemes():
    for url in ("file:///etc/passwd", "gopher://x/", "ftp://internal/"):
        ok, reason = _validate_public_url(url)
        assert ok is False, url
        assert "scheme" in reason


def test_allows_public_ip():
    ok, reason = _validate_public_url("http://8.8.8.8/")
    assert ok is True
    assert reason == "ok"


def test_empty_or_garbage():
    for url in ("", "notaurl", "http://"):
        ok, _ = _validate_public_url(url)
        assert ok is False, url

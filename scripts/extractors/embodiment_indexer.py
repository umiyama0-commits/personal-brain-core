"""Embodiment Indexer — 身体性メタデータ生成器 (skeleton)

⚠️ このスクリプトは Wiki にバイナリを書かない。
   外部ストレージ (S3 / NAS / Drive 等) のインデックスを舐めて
   メタデータ md だけを wiki/embodiment/ に置く。

現状の状態:
- 外部ストレージ未選定 (audit Q-004)。よって本スクリプトは skeleton。
- `--manifest <path>` を指定すると、JSON マニフェストから embodiment md を生成する
  パスは動く。マニフェスト形式は後述。
- audio analysis (speaking_rate / pitch_range 抽出) は別ツール (ffmpeg + Praat 等) で
  事前に行い、その結果をマニフェストに入れて渡す前提。本スクリプト内では分析しない。

manifest 形式 (JSON 配列):
```json
[
  {
    "id_slug": "audio-2026-04-001",
    "modality": "audio",
    "external_path": "s3://owndays-brain-embodiment/audio/2026-04/sample-001.m4a",
    "duration_sec": 1834,
    "context": "新人研修での 30 分講話",
    "emotional_state": "engaged",
    "speaking_rate": "260 syll/min",
    "pitch_range": "120-220 Hz",
    "notable_patterns": ["語尾を上げない", "間を 1.5 秒前後で取る"],
    "training_eligible": "yes",
    "training_eligible_reason": "本人単独発話、機密情報なし、音質良好",
    "recorded_at": "2026-04-15",
    "clone_visibility": "private",
    "exit_visibility": "internal"
  }
]
```

実行:
- skeleton 確認: python3 /app/scripts/extractors/embodiment_indexer.py --print-template
- 実投入:        python3 /app/scripts/extractors/embodiment_indexer.py --manifest <path.json>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # type: ignore  # noqa: E402
    WIKI_DIR,
    ExtractorState,
    log_event,
    next_index,
    render_frontmatter,
    run_context,
    safe_id,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("embodiment_indexer")

LAYER_DIR = WIKI_DIR / "embodiment"
LAYER_DIR.mkdir(parents=True, exist_ok=True)

VALID_MODALITY = {"audio", "video", "both"}
VALID_EMOTION = {"neutral", "engaged", "tired", "amused", "serious", "irritated", "warm"}

# バイナリ拡張子: 万一 wiki に侵入したら検出してエラー
BINARY_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg",
               ".mp4", ".mov", ".webm", ".avi", ".mkv"}


TEMPLATE_MANIFEST = [
    {
        "id_slug": "audio-2026-04-001",
        "modality": "audio",
        "external_path": "s3://owndays-brain-embodiment/audio/2026-04/sample-001.m4a",
        "duration_sec": 1834,
        "context": "新人研修での 30 分講話",
        "emotional_state": "engaged",
        "speaking_rate": "260 syll/min",
        "pitch_range": "120-220 Hz",
        "notable_patterns": ["語尾を上げない", "間を 1.5 秒前後で取る"],
        "training_eligible": "yes",
        "training_eligible_reason": "本人単独発話、機密情報なし、音質良好",
        "recorded_at": "2026-04-15",
        "clone_visibility": "private",
        "exit_visibility": "internal",
    }
]


def _scan_for_binary_intrusion() -> list[Path]:
    """wiki/embodiment/ にバイナリが混入してないか検査。"""
    intruders: list[Path] = []
    if not LAYER_DIR.exists():
        return intruders
    for f in LAYER_DIR.rglob("*"):
        if f.is_file() and f.suffix.lower() in BINARY_EXTS:
            intruders.append(f)
    return intruders


def _validate_entry(e: dict) -> tuple[bool, str]:
    if not isinstance(e, dict):
        return False, "not a dict"
    if e.get("modality") not in VALID_MODALITY:
        return False, f"invalid modality: {e.get('modality')}"
    if not e.get("external_path"):
        return False, "missing external_path"
    ep = e["external_path"]
    # local path はバイナリ拡張子の wiki/ 配下を絶対許可しない
    p = Path(ep)
    try:
        if p.resolve().is_relative_to(WIKI_DIR.resolve()) and p.suffix.lower() in BINARY_EXTS:
            return False, f"external_path points into wiki/: {ep}"
    except Exception:
        pass
    if e.get("emotional_state") not in VALID_EMOTION:
        return False, f"invalid emotional_state: {e.get('emotional_state')}"
    if e.get("training_eligible") not in ("yes", "no"):
        return False, "training_eligible must be yes|no"
    if not e.get("training_eligible_reason"):
        return False, "missing training_eligible_reason"
    if not e.get("recorded_at"):
        return False, "missing recorded_at"
    return True, ""


def _write_entry(entry: dict) -> Path:
    slug = entry.get("id_slug") or "untitled"
    prefix = "embodiment"
    n = next_index(LAYER_DIR, prefix)
    file_id = safe_id(prefix, slug, n)
    out_path = LAYER_DIR / f"{file_id}.md"

    fm = {
        "type": "embodiment_reference",
        "id": file_id,
        "modality": entry["modality"],
        "external_path": entry["external_path"],
        "duration_sec": entry.get("duration_sec", 0),
        "context": entry.get("context", "").replace("\n", " ").strip(),
        "emotional_state": entry["emotional_state"],
        "training_eligible": entry["training_eligible"],
        "training_eligible_reason": entry["training_eligible_reason"].replace("\n", " ").strip(),
        "recorded_at": entry["recorded_at"],
        "last_reviewed": date.today().isoformat(),
        "clone_visibility": entry.get("clone_visibility", "private"),
        "exit_visibility": entry.get("exit_visibility", "internal"),
    }

    notable = entry.get("notable_patterns", [])
    notable_md = "\n".join(f"  - {n}" for n in notable) or "  - (未記述)"

    body = f"""# Embodiment Reference: {entry.get('context', file_id)}

## 文脈
{entry.get('context', '(未記述)')}

## 観察された特徴
- speaking_rate: {entry.get('speaking_rate', '未測定')}
- pitch_range: {entry.get('pitch_range', '未測定')}
- notable_patterns:
{notable_md}

## 感情状態と整合性
- 録音時の感情: {entry['emotional_state']}
- (平常時との乖離は別途レビューで追記)

## 学習適格性
- training_eligible: {entry['training_eligible']}
- 理由: {entry['training_eligible_reason']}

## 関連
- [[style/_index]]
- [[reflex/_index]]
"""
    out = render_frontmatter(fm) + body
    out_path.write_text(out, encoding="utf-8")
    logger.info(
        f"wrote {out_path.relative_to(WIKI_DIR.parent)} (modality={fm['modality']}, "
        f"training_eligible={fm['training_eligible']}, vis={fm['clone_visibility']})"
    )
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description="Embodiment metadata indexer (binaries forbidden)")
    p.add_argument("--manifest", type=Path, help="JSON manifest file path (see module docstring)")
    p.add_argument("--print-template", action="store_true", help="manifest テンプレを stdout に出す")
    p.add_argument("--scan-only", action="store_true", help="バイナリ侵入だけチェックして終了")
    args = p.parse_args()

    if args.print_template:
        print(json.dumps(TEMPLATE_MANIFEST, ensure_ascii=False, indent=2))
        return

    with run_context(
        "embodiment_indexer",
        scan_only=args.scan_only,
        manifest=str(args.manifest) if args.manifest else None,
    ) as ctx:
        intruders = _scan_for_binary_intrusion()
        ctx["intruders"] = len(intruders)
        if intruders:
            logger.error(f"⚠️ wiki/embodiment/ にバイナリ侵入を検出 ({len(intruders)} 件):")
            for f in intruders:
                logger.error(f"  - {f}")
                log_event(
                    "embodiment_indexer",
                    "binary_intrusion",
                    path=str(f),
                )
            logger.error("これらは外部ストレージに移動して external_path で参照してください。")
            ctx["status"] = "binary_intrusion"
            # exit non-zero so cron が気づく
            sys.exit(2)
        else:
            logger.info("binary intrusion scan: OK (バイナリなし)")

        if args.scan_only:
            ctx["status"] = "scan_clean"
            return

        if not args.manifest:
            logger.warning(
                "no --manifest given. このスクリプトは外部ストレージのマニフェスト経由で "
                "メタデータ md を生成する設計です。--print-template でテンプレを表示できます。"
            )
            ctx["status"] = "no_manifest"
            return

        if not args.manifest.exists():
            logger.error(f"manifest file not found: {args.manifest}")
            ctx["status"] = "manifest_missing"
            sys.exit(1)

        try:
            entries = json.loads(args.manifest.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"failed to parse manifest JSON: {e}")
            ctx["status"] = "manifest_parse_failed"
            sys.exit(1)

        if not isinstance(entries, list):
            logger.error("manifest must be a JSON array")
            ctx["status"] = "manifest_not_array"
            sys.exit(1)

        ctx["entries_total"] = len(entries)
        state = ExtractorState.load("embodiment")
        written = 0
        invalid = 0
        for i, e in enumerate(entries):
            ok, reason = _validate_entry(e)
            if not ok:
                invalid += 1
                logger.warning(f"entry[{i}] skipped: {reason}")
                log_event(
                    "embodiment_indexer",
                    "entry_invalid",
                    index=i,
                    reason=reason,
                )
                continue
            try:
                _write_entry(e)
                written += 1
            except Exception as ex:
                logger.error(f"entry[{i}] write failed: {ex}")
                log_event(
                    "embodiment_indexer",
                    "write_failed",
                    index=i,
                    error_msg=str(ex)[:200],
                )

        ctx["items_written"] = written
        ctx["items_invalid"] = invalid

        state.counters["total_written"] = state.counters.get("total_written", 0) + written
        state.save()
        logger.info(f"done. wrote {written} entries.")
        ctx["status"] = "ok"


if __name__ == "__main__":
    main()

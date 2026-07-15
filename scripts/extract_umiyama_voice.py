#!/usr/bin/env python3
"""
海山発言だけを Plaud transcript + 音声 mp3 から抽出して結合する。

入力:
  - data/brain/raw/voice/plaud/*.transcript.md  (Plaud transcript、話者識別付き)
  - data/brain/raw/voice/plaud/audio/*.mp3      (Plaud Web から bulk export した音声、ファイル名は transcript と対応させる)

出力:
  - data/brain/voice_training/umiyama_corpus.mp3  (全議事録の海山ターンを結合した訓練データ)
  - data/brain/voice_training/segments_meta.json  (どの会議からどの時刻が抽出されたか)

使い方:
  python3 scripts/extract_umiyama_voice.py --dry-run   # 切り出し時刻だけ表示
  python3 scripts/extract_umiyama_voice.py             # 実際に ffmpeg で切り出し + 結合
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BRAIN_ROOT = Path("/Users/brain/brain-agent/data/brain")
TRANSCRIPT_DIR = BRAIN_ROOT / "raw" / "voice" / "plaud"
AUDIO_DIR = TRANSCRIPT_DIR / "audio"
OUT_DIR = BRAIN_ROOT / "voice_training"

# Plaud transcript はシリアル番号ラベル (Speaker 1/2/3)、海山特定は wiki/meetings/ frontmatter を参照
WIKI_MEETINGS_DIR = BRAIN_ROOT / "wiki" / "meetings"

# 話者ターン開始行 のパターン: "Speaker 1 00:00:00" / "Speaker 2 00:01:23"
TURN_RE = re.compile(r"^(\S+(?:\s+\S+)?)\s+(\d{2}:\d{2}:\d{2})\s*$")


def get_umiyama_speaker_for(transcript_path: Path) -> "str | None":
    """対応する wiki/meetings/.md の frontmatter から umiyama_speaker を取得。
    例: 'Speaker 1' / 'Speaker 2'。frontmatter 無ければ None。
    """
    stem = transcript_path.stem.replace(".transcript", "")
    wiki_file = WIKI_MEETINGS_DIR / (stem + ".md")
    if not wiki_file.exists():
        return None
    try:
        for line in wiki_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("umiyama_speaker:"):
                return line.split(":", 1)[1].strip()
            if line == "---" and not line.startswith("---"):  # frontmatter 終了
                break
    except Exception:
        pass
    return None


def parse_timestamp(ts: str) -> float:
    """'HH:MM:SS' → seconds"""
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def extract_segments(transcript_path: Path) -> list:
    """transcript から海山ターンの (start, end, speaker) を抽出。

    海山 = wiki/meetings/.md frontmatter の umiyama_speaker (例: 'Speaker 1')。
    """
    umiyama_speaker = get_umiyama_speaker_for(transcript_path)
    if not umiyama_speaker:
        return []

    try:
        text = transcript_path.read_text(encoding="utf-8")
    except Exception:
        return []
    lines = text.splitlines()

    turns = []
    for i, line in enumerate(lines):
        m = TURN_RE.match(line.strip())
        if m:
            turns.append({
                "speaker": m.group(1).strip(),
                "start_sec": parse_timestamp(m.group(2)),
                "line_idx": i,
            })

    for i in range(len(turns) - 1):
        turns[i]["end_sec"] = turns[i + 1]["start_sec"]
    if turns:
        turns[-1]["end_sec"] = None

    # 海山ターンだけ (frontmatter の umiyama_speaker と一致)
    umiyama_turns = [t for t in turns if t["speaker"] == umiyama_speaker]
    return umiyama_turns


def find_audio_for(transcript_path: Path) -> "Path | None":
    """transcript ファイルに対応する mp3 を探す"""
    stem = transcript_path.stem.replace(".transcript", "")
    # 候補: 同名 .mp3 / .m4a / .wav
    for ext in (".mp3", ".m4a", ".wav"):
        cand = AUDIO_DIR / (stem + ext)
        if cand.exists():
            return cand
    # title 一部 match
    for cand in AUDIO_DIR.glob("*"):
        if cand.is_file() and stem[:30] in cand.stem:
            return cand
    return None


def ffmpeg_cut(audio_in: Path, start_sec: float, end_sec: "float | None", out_path: Path) -> bool:
    cmd = ["ffmpeg", "-y", "-i", str(audio_in), "-ss", str(start_sec)]
    if end_sec is not None:
        cmd += ["-to", str(end_sec)]
    cmd += ["-c", "copy", str(out_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.returncode == 0
    except Exception as e:
        print(f"  ffmpeg error: {e}")
        return False


def ffmpeg_concat(parts: list[Path], out_path: Path) -> bool:
    list_file = OUT_DIR / ".concat_list.txt"
    list_file.write_text("\n".join(f"file '{p}'" for p in parts), encoding="utf-8")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception as e:
        print(f"concat error: {e}")
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="切り出し時刻だけ表示、音声処理しない")
    p.add_argument("--out", default=str(OUT_DIR / "umiyama_corpus.mp3"))
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    transcripts = sorted(TRANSCRIPT_DIR.glob("*.transcript.md"))
    print(f"Found {len(transcripts)} transcripts.\n")

    total_seconds = 0.0
    all_segments = []  # for meta
    cut_files = []  # for concat
    seg_counter = 0

    for t_path in transcripts:
        segs = extract_segments(t_path)
        if not segs:
            print(f"⏭️  {t_path.name}: 海山ラベル無し (Voice Profile 未登録の会議?)")
            continue

        audio_path = find_audio_for(t_path)
        meeting_seconds = sum(
            (s["end_sec"] - s["start_sec"]) if s["end_sec"] else 0 for s in segs
        )
        total_seconds += meeting_seconds

        print(f"✅ {t_path.name}")
        print(f"   {len(segs)} 海山ターン / 合計 {meeting_seconds/60:.1f} 分")
        if audio_path:
            print(f"   audio: {audio_path.name}")
        else:
            print(f"   ⚠️ audio mp3 が無い (audio/ に bulk export してね)")

        all_segments.append({
            "transcript": t_path.name,
            "audio": audio_path.name if audio_path else None,
            "umiyama_turns": segs,
            "total_seconds": meeting_seconds,
        })

        if args.dry_run or not audio_path:
            continue

        for s in segs:
            if not s["end_sec"]:
                continue
            seg_counter += 1
            out_p = OUT_DIR / f"_seg_{seg_counter:04d}.mp3"
            if ffmpeg_cut(audio_path, s["start_sec"], s["end_sec"], out_p):
                cut_files.append(out_p)

    print(f"\n=== 合計 ===")
    print(f"海山発言推定: {total_seconds/60:.1f} 分 ({total_seconds/3600:.2f} 時間)")

    # meta 保存
    meta_path = OUT_DIR / "segments_meta.json"
    meta_path.write_text(
        json.dumps({"total_seconds": total_seconds, "segments": all_segments},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"meta: {meta_path}")

    if args.dry_run or not cut_files:
        print("\n(--dry-run / audio 無し のため結合は skip)")
        return

    out_corpus = Path(args.out)
    if ffmpeg_concat(cut_files, out_corpus):
        print(f"\n✅ corpus: {out_corpus}")
        # 個別 seg をクリーンアップ
        for p in cut_files:
            p.unlink(missing_ok=True)
        (OUT_DIR / ".concat_list.txt").unlink(missing_ok=True)
    else:
        print("\n❌ concat 失敗")


if __name__ == "__main__":
    main()

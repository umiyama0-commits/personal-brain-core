"""
Privacy Gate — Brain Wiki 取り込み前のプライバシーフィルタ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

3段階のフィルタリング:
  Gate 1: Rule-based  — 即時ブロック（LLM不要、コスト0）
  Gate 2: LLM判定     — 曖昧なコンテンツを work/personal/ambiguous に分類
  Gate 3: PII除去     — 通過データから個人情報を除去

フロー: data → Gate1 → Gate2 → Gate3 → raw/ (sanitized)
                ↓         ↓
              [drop]   [quarantine/]  ← 後で手動確認
"""

import re
import json
import logging
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path("/app/data/brain")
QUARANTINE_DIR = BRAIN_ROOT / "quarantine"
CONFIG_DIR = BRAIN_ROOT / "privacy"


class Verdict(str, Enum):
    ALLOW = "allow"       # raw/ に取り込む
    BLOCK = "block"       # 即時破棄
    QUARANTINE = "quarantine"  # quarantine/ に保管、後で確認


@dataclass
class FilterResult:
    verdict: Verdict
    gate: str              # どのゲートで判定されたか
    reason: str
    original: str          # 元データ
    sanitized: str = ""    # PII除去後（allowの場合のみ）


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定ファイル（YAML的だがJSON for simplicity）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_CONFIG = {
    # ── 連絡先ブロックリスト ──
    # LINEのユーザーID、メールアドレス、名前の部分一致
    "blocked_contacts": [
        # "wife_line_user_id",
        # "wife@example.com",
        # 必要に応じて追加
    ],

    # ── チャネルブロックリスト ──
    # 特定のLINEグループ、WhatsAppグループをまるごとブロック
    "blocked_channels": [
        # "family_group_id",
        # "hobby_group_id",
    ],

    # ── キーワードブロックリスト ──
    # これらを含むメッセージは即時ブロック（Gate 1）
    "blocked_keywords": [
        # 認証・セキュリティ
        # "パスワード", "暗証番号", "ワンタイム",
    ],

    # ── ホワイトリスト（Gate 2 スキップで即通過）──
    # ほぼ全取り込み方針のため最小限でOK
    "whitelisted_keywords": [],

    # ── LLM分類の設定 ──
    "llm_classify": {
        "enabled": True,
        "model": "fast",  # 低コスト・高速モデルを使用
        "quarantine_on_ambiguous": False,  # 広く取り込む方針: 迷ったら include
    },

    # ── PII パターン ──
    "pii_patterns": {
        "phone": True,
        "email_address": True,
        "credit_card": True,
        "address": True,
        "my_number": True,  # マイナンバー
    },
}


def _load_config() -> dict:
    """設定ファイルを読み込む。なければデフォルトを生成"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_file = CONFIG_DIR / "filter_config.json"
    if config_file.exists():
        return json.loads(config_file.read_text(encoding="utf-8"))
    # 初回: デフォルト設定を書き出し
    config_file.write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return DEFAULT_CONFIG


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Gate 1: Rule-based Filter（即時、LLM不要）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def gate1_rules(
    text: str,
    config: dict,
    sender_id: str = "",
    channel_id: str = "",
) -> Optional[FilterResult]:
    """
    ルールベースの即時フィルタ。
    ブロック対象ならFilterResultを返す。通過ならNone。
    """
    # 連絡先ブロック
    for contact in config.get("blocked_contacts", []):
        if contact and (contact == sender_id or contact in text):
            return FilterResult(
                verdict=Verdict.BLOCK,
                gate="gate1_contact",
                reason=f"Blocked contact: {contact[:10]}...",
                original=text,
            )

    # チャネルブロック
    for channel in config.get("blocked_channels", []):
        if channel and channel == channel_id:
            return FilterResult(
                verdict=Verdict.BLOCK,
                gate="gate1_channel",
                reason=f"Blocked channel: {channel[:10]}...",
                original=text,
            )

    # キーワードブロック
    text_lower = text.lower()
    for keyword in config.get("blocked_keywords", []):
        if keyword and keyword.lower() in text_lower:
            return FilterResult(
                verdict=Verdict.BLOCK,
                gate="gate1_keyword",
                reason=f"Blocked keyword: {keyword}",
                original=text,
            )

    return None  # 通過


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Gate 2: LLM Classifier
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CLASSIFY_PROMPT = """あなたはデータ分類器です。以下のテキストを分析し、JSONのみで応答してください。

【目的】
ユーザーのAIクローンを構築するためのデータ選別。
基本的にほぼすべてのデータを取り込む。
除外するのは以下の5カテゴリのみ。

【exclude — 除外する5カテゴリ】
1. 妻・パートナーとの会話（相手が妻・パートナーであることが明確な場合）
2. 家族間のやりとり（親、子、兄弟との私的な会話）
3. 性的な内容
4. 悪意・悪口・陰口（他者への攻撃的な発言、愚痴で特定個人を貶める内容）
5. 健康・医療の個人的な詳細（症状、診断、服薬、通院）

【include — 上記5つに該当しなければすべて含める】
- 仕事、専門知識、意思決定
- 個人的な意見、価値観、人生観
- 趣味、娯楽、好きなもの
- 学習、読書、技術的な興味
- 友人や同僚との会話（上記5カテゴリに触れていなければ）
- 感情、内省、気づき（愚痴でも特定個人攻撃でなければOK）
- 金融・投資についての考え方（具体的な口座番号等はPII除去で対応）
- AIとの対話での思考過程

【判定の原則】
- 迷ったら include（広く取り込む方針）
- 5カテゴリに明確に該当する場合のみ exclude
- ambiguous は本当に判断不能な場合だけ

【テキスト】
{text}

【出力（JSONのみ）】
{{"classification": "include|exclude|ambiguous", "confidence": 0.0-1.0, "reason": "判定理由"}}
"""


async def gate2_llm_classify(
    text: str,
    config: dict,
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
) -> Optional[FilterResult]:
    """
    LLMによるコンテンツ分類。
    personal → BLOCK, ambiguous → QUARANTINE, work → None(通過)
    """
    llm_config = config.get("llm_classify", {})
    if not llm_config.get("enabled", True):
        return None

    # ホワイトリストに該当すれば即通過（LLMコスト節約）
    text_lower = text.lower()
    for kw in config.get("whitelisted_keywords", []):
        if kw.lower() in text_lower:
            return None

    # 短すぎるメッセージはスキップ（挨拶等）
    if len(text.strip()) < 10:
        return None

    prompt = CLASSIFY_PROMPT.format(text=text[:1000])  # コスト制限

    try:
        resp = await http.post(
            f"{litellm_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {litellm_key}"},
            json={
                "model": llm_config.get("model", "fast"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.0,
            },
        )
        resp.raise_for_status()
        raw_response = resp.json()["choices"][0]["message"]["content"]

        # JSONパース
        cleaned = raw_response.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json")[1].split("```")[0]
        result = json.loads(cleaned)

        classification = result.get("classification", "ambiguous")
        reason = result.get("reason", "")

        if classification == "exclude":
            return FilterResult(
                verdict=Verdict.BLOCK,
                gate="gate2_llm",
                reason=f"Intimate/sensitive: {reason}",
                original=text,
            )
        elif classification == "ambiguous":
            if llm_config.get("quarantine_on_ambiguous", True):
                return FilterResult(
                    verdict=Verdict.QUARANTINE,
                    gate="gate2_llm",
                    reason=f"Ambiguous: {reason}",
                    original=text,
                )

    except Exception as e:
        logger.warning(f"Gate2 LLM classify failed: {e}")
        # ★2026-06-08 システム評価 Security HIGH: LLM 分類が「失敗」した時に素通り (fail-open) すると、
        # 未分類の personal データが公開 clone に載る経路になる。失敗時は QUARANTINE に倒す (fail-safe)。
        # transient error の正常 (work) データは quarantine/ に保管され後から復帰可 = 損失でなく保留。
        return FilterResult(
            verdict=Verdict.QUARANTINE,
            gate="gate2_llm",
            reason=f"classification failed → fail-safe quarantine: {type(e).__name__}",
            original=text,
        )

    return None  # work → 通過


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Gate 3: PII Scrubber
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PII_PATTERNS = {
    "phone": (
        r"(?:\+?81|0)\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}",
        "[電話番号]",
    ),
    "email_address": (
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "[メールアドレス]",
    ),
    "credit_card": (
        r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b",
        "[カード番号]",
    ),
    "my_number": (
        r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}\b",
        "[マイナンバー]",
    ),
    "address": (
        # ★security fix: 旧 `[東京都|大阪府|...県].{2,3}` は文字クラス誤用で住所がマスクされなかった
        #   ([...] 内は alternation でなく文字1個マッチ、`|`/`.{2,3}` も文字クラス内では量化子にならない)。
        #   非キャプチャ alternation + 非貪欲量化子に修正。都道府県(47)→市区町村→番地 を捕捉。
        r"(?:東京都|大阪府|北海道|京都府|.{2,3}県).{1,4}?[市区町村].{1,20}?[0-9\-]+",
        "[住所]",
    ),
}


def gate3_scrub_pii(text: str, config: dict) -> str:
    """通過データからPIIを除去して返す"""
    pii_config = config.get("pii_patterns", {})
    result = text

    for pii_type, (pattern, replacement) in PII_PATTERNS.items():
        if pii_config.get(pii_type, True):
            result = re.sub(pattern, replacement, result)

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メインパイプライン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PrivacyGate:
    """3段階プライバシーフィルタ"""

    def __init__(self, http: httpx.AsyncClient, litellm_url: str, litellm_key: str):
        self.http = http
        self.litellm_url = litellm_url
        self.litellm_key = litellm_key
        self.config = _load_config()
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        self._stats = {"allowed": 0, "blocked": 0, "quarantined": 0}

    def reload_config(self):
        """設定を再読み込み（/filter reload コマンド用）"""
        self.config = _load_config()

    async def filter(
        self,
        text: str,
        sender_id: str = "",
        channel_id: str = "",
    ) -> FilterResult:
        """
        データを3段階フィルタに通す。
        Returns: FilterResult with verdict, sanitized text if allowed.
        """
        # ── Gate 1: Rule-based ──
        result = gate1_rules(text, self.config, sender_id, channel_id)
        if result:
            self._stats["blocked"] += 1
            logger.debug(f"Gate1 blocked: {result.reason}")
            return result

        # ── Gate 2: LLM Classifier ──
        result = await gate2_llm_classify(
            text, self.config, self.http, self.litellm_url, self.litellm_key
        )
        if result:
            if result.verdict == Verdict.QUARANTINE:
                self._save_quarantine(result)
                self._stats["quarantined"] += 1
            else:
                self._stats["blocked"] += 1
            logger.debug(f"Gate2 {result.verdict}: {result.reason}")
            return result

        # ── Gate 3: PII Scrub ──
        sanitized = gate3_scrub_pii(text, self.config)

        self._stats["allowed"] += 1
        return FilterResult(
            verdict=Verdict.ALLOW,
            gate="passed_all",
            reason="All gates passed",
            original=text,
            sanitized=sanitized,
        )

    def _save_quarantine(self, result: FilterResult):
        """quarantine/ に保存（後で手動確認用）"""
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        qfile = QUARANTINE_DIR / f"{ts}.json"
        qfile.write_text(
            json.dumps({
                "gate": result.gate,
                "reason": result.reason,
                "text_preview": result.original[:200],
                "timestamp": ts,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_stats(self) -> dict:
        return {**self._stats, "config_contacts": len(self.config.get("blocked_contacts", []))}

    # ── LINE Bot コマンド用 ──

    async def handle_command(self, message: str) -> Optional[str]:
        """フィルタ関連コマンドを処理"""

        if message.strip() == "/filter":
            s = self.get_stats()
            return (
                f"Privacy Gate status\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Allowed:     {s['allowed']}\n"
                f"Blocked:     {s['blocked']}\n"
                f"Quarantined: {s['quarantined']}\n"
                f"Block contacts: {s['config_contacts']}"
            )

        if message.strip() == "/filter reload":
            self.reload_config()
            return "Privacy config reloaded."

        if message.startswith("/block "):
            target = message[7:].strip()
            self.config.setdefault("blocked_keywords", []).append(target)
            self._save_config()
            return f"Blocked keyword added: {target}"

        if message.startswith("/unblock "):
            target = message[9:].strip()
            keywords = self.config.get("blocked_keywords", [])
            if target in keywords:
                keywords.remove(target)
                self._save_config()
                return f"Unblocked: {target}"
            return f"Not found: {target}"

        if message.strip() == "/quarantine":
            files = sorted(QUARANTINE_DIR.glob("*.json"))[-5:]
            if not files:
                return "Quarantine is empty."
            lines = []
            for f in files:
                data = json.loads(f.read_text(encoding="utf-8"))
                lines.append(f"[{data['timestamp']}] {data['reason']}\n  {data['text_preview'][:80]}...")
            return "Recent quarantine items:\n\n" + "\n\n".join(lines)

        return None

    def _save_config(self):
        config_file = CONFIG_DIR / "filter_config.json"
        config_file.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

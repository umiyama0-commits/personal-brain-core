"""
google_sync.py — Google Calendar + Gmail API 同期

Google API (OAuth2) でカレンダーとメールを取得し、
BrainWikiに取り込む（data/brain/import/ → ファイルウォッチャー経由）。

初回セットアップ:
  1. Google Cloud Console でプロジェクト作成
  2. Calendar API + Gmail API を有効化
  3. OAuth 同意画面を設定（内部 or テスト）
  4. OAuth クライアントID（デスクトップアプリ）を作成
  5. credentials.json をこのディレクトリに配置

使い方:
  python3 google_sync.py --auth              # 初回認証（ブラウザが開く）
  python3 google_sync.py                     # カレンダー+メール両方取得
  python3 google_sync.py --calendar          # カレンダーのみ
  python3 google_sync.py --gmail             # メールのみ
  python3 google_sync.py --dry-run           # プレビューのみ
  python3 google_sync.py --days 3            # 過去3日分
"""

import argparse
import base64
import json
import logging
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

logger = logging.getLogger(__name__)

# ─── パス設定 ───
BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "data" / "brain" / "import"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "data" / "brain" / ".google_token.json"

# Calendar + Gmail + Drive の読み取り権限
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

DOWNLOAD_DIR = BASE_DIR / "data" / "brain" / ".calendar_attachments"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OAuth2 認証
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_credentials() -> Credentials:
    """OAuth2 トークンを取得（キャッシュ or リフレッシュ or 新規認証）"""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("トークンをリフレッシュ中...")
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        return creds

    # 新規認証
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"credentials.json が見つかりません: {CREDENTIALS_FILE}\n"
            "Google Cloud Console で OAuth クライアントID を作成し、\n"
            "credentials.json をダウンロードしてください。"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    logger.info(f"トークン保存: {TOKEN_FILE}")

    return creds


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Google Calendar
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def sync_calendar(creds: Credentials, days: int = 1, dry_run: bool = False) -> list[dict]:
    """Google Calendar API で予定を取得"""
    service = build("calendar", "v3", credentials=creds)

    now = datetime.utcnow()
    time_min = (now - timedelta(days=max(0, days - 1))).replace(hour=0, minute=0, second=0).isoformat() + "Z"
    time_max = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59).isoformat() + "Z"

    logger.info(f"カレンダー: {time_min[:10]} 〜 {time_max[:10]} の予定を取得中...")

    # 全カレンダーのリストを取得
    calendar_list = service.calendarList().list().execute()
    calendars = calendar_list.get("items", [])
    logger.info(f"  カレンダー数: {len(calendars)}")

    all_events = []
    for cal in calendars:
        cal_id = cal["id"]
        cal_name = cal.get("summary", cal_id)

        try:
            events_result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            ).execute()
        except Exception as e:
            logger.warning(f"  カレンダー '{cal_name}' の取得エラー: {e}")
            continue

        events = events_result.get("items", [])
        for ev in events:
            start = ev.get("start", {})
            end = ev.get("end", {})
            start_str = start.get("dateTime", start.get("date", ""))
            end_str = end.get("dateTime", end.get("date", ""))

            # 添付ファイル情報を取得
            attachments = []
            for att in ev.get("attachments", []):
                attachments.append({
                    "title": att.get("title", ""),
                    "mimeType": att.get("mimeType", ""),
                    "fileUrl": att.get("fileUrl", ""),
                    "fileId": att.get("fileId", ""),
                })

            # description 内の Google Drive / Docs リンクも抽出
            desc = ev.get("description", "") or ""
            drive_links = re.findall(
                r'https://(?:docs|drive|sheets)\.google\.com/[^\s<"\']+', desc
            )

            event_data = {
                "calendar": cal_name,
                "summary": ev.get("summary", "(無題)"),
                "start": start_str,
                "end": end_str,
                "location": ev.get("location", ""),
                "description": desc[:500],
                "attendees": [
                    a.get("email", "") for a in ev.get("attendees", [])
                ],
                "status": ev.get("status", ""),
                "organizer": ev.get("organizer", {}).get("email", ""),
                "attachments": attachments,
                "drive_links": drive_links,
            }
            all_events.append(event_data)

    logger.info(f"  → {len(all_events)} 件の予定")

    if dry_run:
        for ev in all_events:
            t = ev["start"][11:16] if "T" in ev["start"] else "終日"
            print(f"  [{t}] {ev['summary']} ({ev['calendar']})")
        return all_events

    # 添付ファイルのダウンロード＆テキスト抽出
    if not dry_run:
        drive_service = build("drive", "v3", credentials=creds)
        for ev in all_events:
            extracted = _extract_event_attachments(ev, drive_service, creds)
            ev["extracted_content"] = extracted

    if all_events:
        _save_calendar_events(all_events, days)

    return all_events


def _extract_event_attachments(event: dict, drive_service, creds) -> list[str]:
    """イベントの添付ファイル・Driveリンクからテキストを抽出"""
    from content_extractor import extract_file_text, _get_gdoc_export_url
    import asyncio

    extracted = []
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Calendar API の attachments フィールド
    for att in event.get("attachments", []):
        file_id = att.get("fileId", "")
        title = att.get("title", "unknown")
        mime = att.get("mimeType", "")

        if not file_id:
            continue

        try:
            text = _download_and_extract(drive_service, file_id, title, mime)
            if text:
                extracted.append(f"[添付: {title}]\n{text}")
                logger.info(f"  添付抽出: {title} → {len(text)} chars")
        except Exception as e:
            logger.warning(f"  添付抽出エラー ({title}): {e}")

    # 2. description 内の Google Drive/Docs リンク
    for link in event.get("drive_links", []):
        try:
            # Google Docs/Sheets → エクスポートURL経由
            export_url = _get_gdoc_export_url(link)
            if export_url:
                import httpx
                resp = httpx.get(export_url, follow_redirects=True, timeout=30.0,
                                headers={"Authorization": f"Bearer {creds.token}"})
                if resp.status_code == 200:
                    text = resp.text[:5000]
                    extracted.append(f"[リンク: {link[:60]}]\n{text}")
                    logger.info(f"  リンク抽出: {link[:40]} → {len(text)} chars")
        except Exception as e:
            logger.warning(f"  リンク抽出エラー: {e}")

    return extracted


def _download_and_extract(drive_service, file_id: str, title: str, mime_type: str) -> str:
    """Google Drive からファイルをダウンロードしてテキスト抽出"""
    from content_extractor import extract_file_text
    import asyncio

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Google Sheets → 全シートをCSVで取得
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return _extract_all_sheets(drive_service, file_id, title)

    # Google Docs/Slides → テキストでエクスポート
    google_export_map = {
        "application/vnd.google-apps.document": ("text/plain", ".txt"),
        "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    }

    if mime_type in google_export_map:
        export_mime, ext = google_export_map[mime_type]
        request = drive_service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = drive_service.files().get_media(fileId=file_id)
        ext = Path(title).suffix or ".bin"

    # ダウンロード
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    # テキスト形式はそのまま返す
    if mime_type in google_export_map:
        text = fh.getvalue().decode("utf-8", errors="replace")
        return text[:20000]

    # バイナリファイルは一時保存してextract
    tmp_path = DOWNLOAD_DIR / f"{file_id}{ext}"
    tmp_path.write_bytes(fh.getvalue())

    try:
        loop = asyncio.new_event_loop()
        text = loop.run_until_complete(extract_file_text(tmp_path))
        loop.close()
        return text or ""
    finally:
        tmp_path.unlink(missing_ok=True)


def _extract_all_sheets(drive_service, file_id: str, title: str) -> str:
    """Google Sheetsの全シートを認証付きCSVエクスポートで取得"""
    import requests as _requests

    creds = get_credentials()
    all_text = []

    # gid=0から順に試す。シート名は不明だがデータは取れる
    for gid_idx, gid in enumerate([0, 1, 2, 3, 4]):
        try:
            export_url = (
                f"https://docs.google.com/spreadsheets/d/{file_id}"
                f"/export?format=csv&gid={gid}"
            )
            resp = _requests.get(
                export_url,
                headers={"Authorization": f"Bearer {creds.token}"},
                timeout=15,
            )
            if resp.status_code == 200 and len(resp.content) > 10:
                csv_text = resp.content.decode("utf-8", errors="replace").strip()
                if csv_text:
                    all_text.append(f"[Sheet {gid_idx + 1}]\n{csv_text}")
                    logger.info(f"  Sheet gid={gid}: {len(csv_text)} chars")
            else:
                break  # これ以上シートがない
        except Exception as e:
            logger.warning(f"  Sheet gid={gid} error: {e}")
            break

    if all_text:
        result = "\n\n".join(all_text)
        return result[:20000]

    # フォールバック: Drive API CSVエクスポート
    logger.info(f"  Falling back to Drive API CSV export for {title}")
    fh = io.BytesIO()
    request = drive_service.files().export_media(fileId=file_id, mimeType="text/csv")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    text = fh.getvalue().decode("utf-8", errors="replace")
    return text[:20000]


def _save_calendar_events(events: list[dict], days: int):
    """カレンダーイベントをファイルに保存"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    filepath = OUTPUT_DIR / f"gcal_{today_str}.txt"

    lines = [
        f"[Google Calendar] {today_str}",
        f"取得範囲: 過去{days}日",
        "",
    ]

    # 日付でグループ化
    by_date = {}
    for ev in events:
        d = ev["start"][:10] if ev["start"] else "unknown"
        by_date.setdefault(d, []).append(ev)

    for d in sorted(by_date.keys()):
        lines.append(f"## {d}")
        lines.append("")
        for ev in by_date[d]:
            if "T" in ev["start"]:
                start_t = ev["start"][11:16]
                end_t = ev["end"][11:16] if "T" in ev["end"] else ""
                time_str = f"{start_t}-{end_t}" if end_t else start_t
            else:
                time_str = "終日"

            lines.append(f"- [{time_str}] {ev['summary']}")
            if ev["calendar"] != "primary":
                lines.append(f"  カレンダー: {ev['calendar']}")
            if ev["location"]:
                lines.append(f"  場所: {ev['location']}")
            if ev["attendees"]:
                lines.append(f"  参加者: {', '.join(ev['attendees'][:5])}")
            if ev["description"]:
                desc = ev["description"].replace("\n", " ")[:200]
                lines.append(f"  詳細: {desc}")
            # 添付ファイルの抽出コンテンツ
            for content in ev.get("extracted_content", []):
                lines.append(f"  {content.replace(chr(10), chr(10) + '  ')}")
            lines.append("")
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"  保存: {filepath.name}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Gmail
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def sync_gmail(creds: Credentials, days: int = 1, max_emails: int = 30, dry_run: bool = False) -> list[dict]:
    """Gmail API でメールを取得"""
    service = build("gmail", "v1", credentials=creds)

    after_date = (date.today() - timedelta(days=days)).isoformat().replace("-", "/")
    # プロモーション・ソーシャル・フォーラムを除外し、自動通知系も除外
    query = (
        f"after:{after_date} "
        f"-category:promotions -category:social -category:forums "
        f"-from:noreply -from:no-reply -from:notification"
    )

    logger.info(f"Gmail: {query} のメールを取得中...")

    # メール一覧を取得
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_emails
    ).execute()

    message_ids = results.get("messages", [])
    logger.info(f"  → {len(message_ids)} 件のメール")

    # 自動通知・ノイズ送信元のスキップリスト
    SKIP_SENDERS = {
        "noreply", "no-reply", "mailer-daemon", "postmaster",
        "notification", "alert", "donotreply", "do-not-reply",
    }
    SKIP_DOMAINS = {
        "docusign.net", "bizreach.co.jp", "firstbank.com.tw",
        "peoplehum.com",
    }

    emails = []
    for i, msg_ref in enumerate(message_ids):
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
        except Exception as e:
            logger.warning(f"  メール取得エラー [{i}]: {e}")
            continue

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

        # 自動通知系の送信元をスキップ
        from_header = headers.get("From", "").lower()
        sender_local = from_header.split("@")[0].split("<")[-1].strip() if "@" in from_header else ""
        sender_domain = from_header.split("@")[-1].rstrip(">").strip() if "@" in from_header else ""
        if any(skip in sender_local for skip in SKIP_SENDERS):
            continue
        if sender_domain in SKIP_DOMAINS:
            continue

        is_unread = "UNREAD" in msg.get("labelIds", [])

        # 本文を抽出
        body = _extract_body(msg.get("payload", {}))

        email_data = {
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", "(件名なし)"),
            "date": headers.get("Date", ""),
            "unread": is_unread,
            "snippet": msg.get("snippet", ""),
            "body": body[:2000],
            "labels": msg.get("labelIds", []),
        }
        emails.append(email_data)

    if dry_run:
        for em in emails:
            unread = "*" if em["unread"] else " "
            sender = _parse_sender(em["from"])
            print(f"  {unread} {sender[:25]:25} | {em['subject'][:45]} | {em['date'][:16]}")
        return emails

    if emails:
        _save_emails(emails, days)

    return emails


def _extract_body(payload: dict) -> str:
    """メール本文をプレーンテキストで抽出"""
    # シンプルなメール
    if payload.get("mimeType") == "text/plain" and "body" in payload:
        data = payload["body"].get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # マルチパートメール
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # HTML フォールバック（タグ除去）
    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", "", html)[:2000]

    # ネストされたマルチパート
    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            result = _extract_body(part)
            if result:
                return result

    return ""


def _parse_sender(from_header: str) -> str:
    """'Name <email>' から名前を抽出"""
    if "<" in from_header:
        return from_header.split("<")[0].strip().strip('"')
    return from_header


def _save_emails(emails: list[dict], days: int):
    """メールをファイルに保存"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    filepath = OUTPUT_DIR / f"gmail_{today_str}.txt"

    lines = [
        f"[Gmail] {today_str}",
        f"取得範囲: 過去{days}日 / {len(emails)}件",
        "",
    ]

    for em in emails:
        direction = "未読" if em["unread"] else "既読"
        lines.append(f"[{direction}] {em['subject']}")
        lines.append(f"From: {em['from']}")
        lines.append(f"To: {em['to']}")
        lines.append(f"Date: {em['date']}")
        if em["body"]:
            lines.append("---")
            lines.append(em["body"][:1500])
        elif em["snippet"]:
            lines.append(f"概要: {em['snippet']}")
        lines.append("")
        lines.append("=" * 40)
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"  保存: {filepath.name}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(description="Google Calendar + Gmail API 同期")
    parser.add_argument("--calendar", action="store_true", help="カレンダーのみ")
    parser.add_argument("--gmail", action="store_true", help="Gmailのみ")
    parser.add_argument("--auth", action="store_true", help="認証のみ（トークン取得）")
    parser.add_argument("--dry-run", action="store_true", help="プレビューのみ")
    parser.add_argument("--days", type=int, default=1, help="取得日数（デフォルト: 1）")
    parser.add_argument("--max-emails", type=int, default=30, help="最大メール数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    # 認証
    creds = get_credentials()
    logger.info(f"認証OK（トークン有効期限: {creds.expiry}）")

    if args.auth:
        logger.info("認証完了。トークンが保存されました。")
        return

    do_calendar = args.calendar or (not args.calendar and not args.gmail)
    do_gmail = args.gmail or (not args.calendar and not args.gmail)

    if do_calendar:
        sync_calendar(creds, days=args.days, dry_run=args.dry_run)

    if do_gmail:
        sync_gmail(creds, days=args.days, max_emails=args.max_emails, dry_run=args.dry_run)

    logger.info("Google同期完了")


if __name__ == "__main__":
    main()

"""MD → HTML → PDF converter (= Chrome headless 使用、Japanese font 対応).

Usage:
  python3 md_to_pdf.py <input.md> <output.pdf>
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown


def md_to_html(md_path: Path) -> str:
    """Markdown → styled HTML (= 日本語 + 印刷向け CSS)."""
    md_text = md_path.read_text(encoding="utf-8")

    # markdown 拡張: table / fenced_code / toc / nl2br
    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "nl2br", "sane_lists"],
    )

    title = md_path.stem.replace("_", " ")

    css = """
@page {
  size: A4;
  margin: 18mm 16mm 18mm 16mm;
  @bottom-right {
    content: counter(page) " / " counter(pages);
    font-family: -apple-system, "Hiragino Sans", sans-serif;
    font-size: 9pt;
    color: #6b7280;
  }
}

* { box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Helvetica Neue", sans-serif;
  font-size: 10.5pt;
  line-height: 1.65;
  color: #1f2937;
  max-width: 100%;
  margin: 0;
  padding: 0;
}

h1 {
  font-size: 22pt;
  font-weight: 700;
  margin: 24px 0 12px;
  padding-bottom: 10px;
  border-bottom: 3px solid #1f2937;
  page-break-before: auto;
  page-break-after: avoid;
}

h1:first-of-type {
  page-break-before: avoid;
}

h2 {
  font-size: 15pt;
  font-weight: 700;
  margin: 28px 0 10px;
  padding-bottom: 6px;
  border-bottom: 2px solid #e5e7eb;
  page-break-after: avoid;
  color: #1f2937;
}

h3 {
  font-size: 12pt;
  font-weight: 600;
  margin: 18px 0 8px;
  color: #374151;
  page-break-after: avoid;
}

h4 {
  font-size: 10.5pt;
  font-weight: 600;
  margin: 14px 0 6px;
  color: #4b5563;
  page-break-after: avoid;
}

p {
  margin: 8px 0;
  text-align: justify;
}

ul, ol {
  margin: 8px 0;
  padding-left: 24px;
}

li {
  margin: 3px 0;
}

code {
  font-family: "SF Mono", "Menlo", "Consolas", monospace;
  font-size: 9pt;
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 3px;
  color: #be185d;
}

pre {
  background: #f9fafb;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 12px 14px;
  overflow-x: auto;
  font-size: 9pt;
  line-height: 1.5;
  page-break-inside: avoid;
  margin: 12px 0;
}

pre code {
  background: transparent;
  padding: 0;
  color: #1f2937;
  font-size: 9pt;
}

table {
  border-collapse: collapse;
  width: 100%;
  margin: 12px 0;
  font-size: 9.5pt;
  page-break-inside: avoid;
}

th, td {
  padding: 6px 10px;
  text-align: left;
  border: 1px solid #e5e7eb;
  vertical-align: top;
}

th {
  background: #f9fafb;
  font-weight: 600;
  color: #1f2937;
}

tr:nth-child(even) td {
  background: #fafafa;
}

blockquote {
  border-left: 4px solid #3b82f6;
  padding: 8px 14px;
  margin: 12px 0;
  background: #f9fafb;
  color: #4b5563;
  font-style: italic;
}

hr {
  border: none;
  border-top: 1px solid #e5e7eb;
  margin: 24px 0;
  page-break-after: auto;
}

a {
  color: #2563eb;
  text-decoration: none;
}

strong, b {
  font-weight: 700;
  color: #1f2937;
}

em, i {
  font-style: italic;
}

/* 強制改頁: ## TL;DR の前 / 各章の前で */
h1 + h1, h1 + p + h1 {
  page-break-before: always;
}

/* Closing 強調 */
h1:last-of-type {
  border-top: 3px solid #3b82f6;
  border-bottom: none;
  padding-top: 12px;
  margin-top: 32px;
}
"""

    html_doc = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
{html_body}
</body>
</html>
"""
    return html_doc


def html_to_pdf_chrome(html_path: Path, pdf_path: Path) -> None:
    """Chrome headless で HTML → PDF."""
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        f"file://{html_path}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"Chrome stderr: {result.stderr[:500]}", file=sys.stderr)
        raise RuntimeError(f"chrome failed: exit {result.returncode}")


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 md_to_pdf.py <input.md> <output.pdf>", file=sys.stderr)
        sys.exit(1)

    md_path = Path(sys.argv[1]).resolve()
    pdf_path = Path(sys.argv[2]).resolve()

    if not md_path.exists():
        print(f"input md not found: {md_path}", file=sys.stderr)
        sys.exit(2)

    print(f"converting: {md_path} → {pdf_path}", file=sys.stderr)
    html_doc = md_to_html(md_path)

    # 一時 HTML file 経由 (Chrome は file:// で読む)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".html", delete=False,
    ) as tmp:
        tmp.write(html_doc)
        tmp_html = Path(tmp.name)

    try:
        html_to_pdf_chrome(tmp_html, pdf_path)
        print(f"OK: {pdf_path} ({pdf_path.stat().st_size:,} bytes)", file=sys.stderr)
    finally:
        tmp_html.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

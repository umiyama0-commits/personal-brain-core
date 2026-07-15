// ─── Personal Brain 共通ユーティリティ ───
// chat.html / dashboard.html 両方から読み込む

(function (global) {
  'use strict';

  // ─── Auth ───
  const KEY_STORAGE = 'brain_key';
  const SESSION_STORAGE = 'brain_session_id';

  function getKey() {
    return localStorage.getItem(KEY_STORAGE) || '';
  }
  function setKey(k) {
    localStorage.setItem(KEY_STORAGE, k);
  }
  function clearKey() {
    localStorage.removeItem(KEY_STORAGE);
    localStorage.removeItem(SESSION_STORAGE);
  }
  function getSessionId() {
    let sid = localStorage.getItem(SESSION_STORAGE);
    if (!sid) {
      sid = 'web_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
      localStorage.setItem(SESSION_STORAGE, sid);
    }
    return sid;
  }

  // ─── Fetch helpers ───
  async function apiGet(url, { signal } = {}) {
    const key = getKey();
    const resp = await fetch(url, {
      headers: { Authorization: 'Bearer ' + key },
      signal,
    });
    if (resp.status === 401 || resp.status === 403) {
      clearKey();
      location.reload();
      throw new Error('auth');
    }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }

  async function apiPost(url, body, { signal } = {}) {
    const key = getKey();
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body || {}),
      signal,
    });
    if (resp.status === 401 || resp.status === 403) {
      clearKey();
      location.reload();
      throw new Error('auth');
    }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }

  // ─── HTML escape ───
  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ─── Safe URL validation ───
  const SAFE_URL_RE = /^(https?:|mailto:|\/)/i;
  function safeUrl(url) {
    return SAFE_URL_RE.test(url) ? url : '';
  }

  // ─── Markdown renderer (XSS-safe) ───
  function renderMarkdown(text) {
    if (text == null) return '';
    // 1. Fully escape all input first
    let html = escHtml(text);

    // 2. Code blocks (preserve code content as-is within pre/code)
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
      // code is already escaped
      return '<pre><code>' + code.trim() + '</code></pre>';
    });

    // 3. Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // 4. Bold / italic
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?<!\*)\*(?!\*)([^\*]+?)\*(?!\*)/g, '<em>$1</em>');

    // 5. Markdown links [text](url) — validate URL
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, txt, url) => {
      const safe = safeUrl(url);
      if (!safe) return txt;
      return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>';
    });

    // 6. Bare URLs (escape quotes that could break attribute)
    html = html.replace(/(?<!["'>])(https?:\/\/[^\s<>"']+)/g, (m, url) => {
      const safe = safeUrl(url);
      if (!safe) return m;
      return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + safe + '</a>';
    });

    // 7. Headings (within message)
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');

    // 8. Lists
    html = html.replace(/^[•\-]\s+(.+)$/gm, '<li>$1</li>');
    html = html.replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>[\s\S]*?<\/li>\s*)+/g, m => '<ul>' + m + '</ul>');

    // 9. Tables (GFM style)
    html = html.replace(/^(\|.+\|)\n(\|[-| :]+\|)\n((?:\|.+\|\n?)*)/gm, (_, header, sep, body) => {
      const ths = header.split('|').filter(c => c.trim()).map(c => '<th>' + c.trim() + '</th>').join('');
      const rows = body.trim().split('\n').map(r => {
        const tds = r.split('|').filter(c => c.trim()).map(c => '<td>' + c.trim() + '</td>').join('');
        return '<tr>' + tds + '</tr>';
      }).join('');
      return '<table><thead><tr>' + ths + '</tr></thead><tbody>' + rows + '</tbody></table>';
    });

    // 10. Paragraphs
    html = html.split(/\n{2,}/).map(p => {
      const trimmed = p.trim();
      if (!trimmed) return '';
      if (/^<(h\d|ul|ol|pre|table|blockquote)/.test(trimmed)) return trimmed;
      return '<p>' + trimmed.replace(/\n/g, '<br>') + '</p>';
    }).join('');

    return html;
  }

  // ─── Relative time ───
  function relTime(isoOrTs) {
    if (!isoOrTs) return '';
    const t = typeof isoOrTs === 'number' ? isoOrTs : Date.parse(isoOrTs);
    if (isNaN(t)) return '';
    const diff = (Date.now() - t) / 1000;
    if (diff < 60) return 'たった今';
    if (diff < 3600) return Math.floor(diff / 60) + '分前';
    if (diff < 86400) return Math.floor(diff / 3600) + '時間前';
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + '日前';
    return new Date(t).toLocaleDateString('ja-JP', { month: 'short', day: 'numeric' });
  }

  // ─── iOS keyboard / viewport helper ───
  function bindVisualViewport(scrollTarget) {
    if (!window.visualViewport) return;
    const onResize = () => {
      if (scrollTarget) scrollTarget.scrollTop = scrollTarget.scrollHeight;
    };
    window.visualViewport.addEventListener('resize', onResize);
    window.visualViewport.addEventListener('scroll', onResize);
  }

  // ─── Visibility-aware interval ───
  function pausableInterval(fn, ms) {
    let id = null;
    function start() {
      if (id) return;
      id = setInterval(fn, ms);
    }
    function stop() {
      if (id) clearInterval(id);
      id = null;
    }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stop();
      else { fn(); start(); }
    });
    start();
    return { start, stop };
  }

  // ─── PWA SW registration ───
  function registerSW() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/static/sw.js').catch(() => {});
    }
  }

  global.Brain = {
    getKey, setKey, clearKey, getSessionId,
    apiGet, apiPost,
    escHtml, safeUrl, renderMarkdown,
    relTime,
    bindVisualViewport, pausableInterval,
    registerSW,
  };
})(window);

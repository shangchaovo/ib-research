"""FResearch 统一设计系统。

首页（`ib_research_server.py`）和 SEO/GEO 子页（`ib_research_geo.py`）以前各自维护
一份 token 和组件样式，同一个色板被抄了两遍、子页也拿不到主题切换。这里是单一源头：

- ``TOKENS_CSS``      —— 字体、四套主题的 CSS 变量、圆角与缓动
- ``BASE_CSS``        —— reset、页面底噪与光晕、``.wrap``、排版工具类
- ``COMPONENTS_CSS``  —— 导航、面包屑、面板、表格、徽章、chip、指标、页脚等公共组件
- ``PAGE_CSS``        —— 以上三者拼接，任何整页都应该只引这一份

首页在 ``PAGE_CSS`` 之后再追加自己的专属样式（hero、资产网格、工具栏等）。
"""

TOKENS_CSS = """
:root {
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
    --font-serif: "Iowan Old Style", "Songti SC", "STSong", Georgia, serif;
    --font-mono: "SFMono-Regular", "Cascadia Mono", Consolas, "Liberation Mono", monospace;
    --bg: #08080a;
    --bg-elevated: #0c0c10;
    --surface: #12121a;
    --surface-2: #1a1a24;
    --surface-3: #22222e;
    --border: rgba(232, 230, 227, 0.07);
    --border-strong: rgba(232, 230, 227, 0.15);
    --text: #f2f0ec;
    --text-secondary: #a8a5a0;
    --text-tertiary: #7a7770;
    --gold: #c9a45c;
    --gold-bright: #d4af37;
    --gold-dim: rgba(201, 164, 92, 0.10);
    --gold-glow: rgba(201, 164, 92, 0.22);
    --bull: #4ade80;
    --bear: #f87171;
    --neutral: #fbbf24;
    --accent: #7dd3fc;
    --shadow-sm: 0 4px 12px rgba(0, 0, 0, 0.25);
    --shadow-md: 0 8px 30px rgba(0, 0, 0, 0.35);
    --shadow-lg: 0 20px 60px rgba(0, 0, 0, 0.45);
    --radius-sm: 8px;
    --radius-md: 14px;
    --radius-lg: 20px;
    --radius-xl: 28px;
    --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
    --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);
}
:root[data-theme="light"] {
    --bg: #f7f5f0;
    --bg-elevated: #ffffff;
    --surface: #ffffff;
    --surface-2: #f2efe9;
    --surface-3: #e8e4dc;
    --border: rgba(30, 25, 18, 0.10);
    --border-strong: rgba(30, 25, 18, 0.18);
    --text: #1e1912;
    --text-secondary: #554f45;
    --text-tertiary: #7d7669;
    --gold: #8b6914;
    --gold-bright: #a67c00;
    --gold-dim: rgba(139, 105, 20, 0.08);
    --gold-glow: rgba(139, 105, 20, 0.15);
    --bull: #15803d;
    --bear: #b91c1c;
    --neutral: #a16207;
    --accent: #0369a1;
    --shadow-sm: 0 4px 12px rgba(30, 25, 18, 0.08);
    --shadow-md: 0 8px 30px rgba(30, 25, 18, 0.10);
    --shadow-lg: 0 20px 60px rgba(30, 25, 18, 0.12);
}
:root[data-theme="sepia"] {
    --bg: #f0e9db;
    --bg-elevated: #faf5eb;
    --surface: #faf5eb;
    --surface-2: #e9e0cd;
    --surface-3: #ded3bd;
    --border: rgba(60, 48, 30, 0.12);
    --border-strong: rgba(60, 48, 30, 0.22);
    --text: #2e2418;
    --text-secondary: #57482f;
    --text-tertiary: #806c56;
    --gold: #6b4c1e;
    --gold-bright: #7d5a24;
    --gold-dim: rgba(107, 76, 30, 0.10);
    --gold-glow: rgba(107, 76, 30, 0.15);
    --bull: #3f6212;
    --bear: #991b1b;
    --neutral: #854d0e;
    --accent: #1e40af;
    --shadow-sm: 0 4px 12px rgba(60, 48, 30, 0.08);
    --shadow-md: 0 8px 30px rgba(60, 48, 30, 0.10);
    --shadow-lg: 0 20px 60px rgba(60, 48, 30, 0.12);
}
:root[data-theme="dark"] {
    --bg: #08080a;
    --bg-elevated: #0c0c10;
    --surface: #12121a;
    --surface-2: #1a1a24;
    --surface-3: #22222e;
    --border: rgba(232, 230, 227, 0.07);
    --border-strong: rgba(232, 230, 227, 0.15);
    --text: #f2f0ec;
    --text-secondary: #a8a5a0;
    --text-tertiary: #7a7770;
    --gold: #c9a45c;
    --gold-bright: #d4af37;
    --gold-dim: rgba(201, 164, 92, 0.10);
    --gold-glow: rgba(201, 164, 92, 0.22);
    --bull: #4ade80;
    --bear: #f87171;
    --neutral: #fbbf24;
    --accent: #7dd3fc;
    --shadow-sm: 0 4px 12px rgba(0, 0, 0, 0.25);
    --shadow-md: 0 8px 30px rgba(0, 0, 0, 0.35);
    --shadow-lg: 0 20px 60px rgba(0, 0, 0, 0.45);
}
"""

BASE_CSS = """
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-sans);
    line-height: 1.65;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    min-height: 100vh;
}
body::before {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 400 400' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)'/%3E%3C/svg%3E");
    opacity: 0.035;
    mix-blend-mode: overlay;
}
body::after {
    content: "";
    position: fixed;
    top: -20%;
    left: -10%;
    width: 60vw;
    height: 60vw;
    pointer-events: none;
    z-index: 0;
    background: radial-gradient(circle, var(--gold-glow) 0%, transparent 55%);
    filter: blur(100px);
    opacity: 0.6;
}
.wrap {
    position: relative;
    z-index: 1;
    max-width: 1180px;
    margin: 0 auto;
    padding: 40px 28px 96px;
}
.serif { font-family: var(--font-serif); }
.mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.muted { color: var(--text-secondary); }
a { color: var(--gold); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible {
    outline: 2px solid var(--gold);
    outline-offset: 2px;
    border-radius: 4px;
}
.sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
}
@keyframes fadeUp {
    from { opacity: 0; transform: translateY(24px); }
    to { opacity: 1; transform: translateY(0); }
}
.reveal { animation: fadeUp 0.7s var(--ease-out) both; }
.reveal-delay-1 { animation-delay: 0.06s; }
.reveal-delay-2 { animation-delay: 0.12s; }
.reveal-delay-3 { animation-delay: 0.18s; }
.reveal-delay-4 { animation-delay: 0.24s; }
.reveal-delay-5 { animation-delay: 0.30s; }
@media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    .reveal { animation: none; }
    * { transition-duration: 0.01ms !important; }
}
"""

COMPONENTS_CSS = """
/* ---- Site chrome ---------------------------------------------------- */
.site-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 22px;
    align-items: baseline;
    margin-bottom: 26px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
}
.site-nav .brand {
    font-family: var(--font-serif);
    font-size: 21px;
    letter-spacing: 0.01em;
    color: var(--text);
    margin-right: 6px;
}
.site-nav a {
    color: var(--text-secondary);
    font-size: 13.5px;
    transition: color 0.2s var(--ease-out);
}
.site-nav a:hover { color: var(--gold); text-decoration: none; }
.site-nav a[aria-current="page"] { color: var(--gold); }
.site-nav .nav-spacer { flex: 1 1 auto; }
.footer-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 18px;
    margin-top: 40px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
    font-size: 13px;
}
.footer-nav a { color: var(--text-secondary); }
.crumb { font-size: 12.5px; color: var(--text-tertiary); margin-bottom: 20px; }
.crumb a { color: var(--text-secondary); }

/* ---- Theme switcher ------------------------------------------------- */
.theme-switcher {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    padding: 3px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 999px;
}
.theme-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: var(--text-tertiary);
    cursor: pointer;
    transition: background 0.2s var(--ease-out), color 0.2s var(--ease-out);
}
.theme-btn svg { width: 15px; height: 15px; }
.theme-btn:hover { color: var(--text); }
.theme-btn.active { background: var(--gold); color: var(--bg); }

/* ---- Headings & sections ------------------------------------------- */
h1 {
    font-family: var(--font-serif);
    font-size: clamp(30px, 4.4vw, 46px);
    line-height: 1.18;
    letter-spacing: -0.01em;
    margin: 0 0 14px;
}
h2 { font-size: 19px; margin: 0 0 12px; letter-spacing: 0.01em; }
h3 { font-size: 15px; margin: 0 0 8px; }
p { margin: 0 0 12px; }
.one-liner { font-size: 17.5px; color: var(--text); margin: 0 0 18px; max-width: 68ch; }
.lede { color: var(--text-secondary); margin: 0 0 26px; max-width: 74ch; }
.section { margin: 0 0 56px; }
.section-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 18px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
}
.section-title {
    font-family: var(--font-serif);
    font-size: clamp(21px, 2.4vw, 27px);
    font-weight: 500;
    line-height: 1.25;
    margin: 0;
    color: var(--text);
}
.section-count {
    flex: 0 0 auto;
    font-family: var(--font-mono);
    font-size: 11.5px;
    letter-spacing: 0.06em;
    color: var(--text-tertiary);
    white-space: nowrap;
}
.section-intro {
    color: var(--text-secondary);
    font-size: 14.5px;
    margin: 0 0 20px;
    max-width: 82ch;
}
.eyebrow {
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--gold);
    margin: 0 0 10px;
}

/* ---- Panels --------------------------------------------------------- */
.panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 24px 26px;
    margin: 0 0 18px;
}
.panel > h2 { color: var(--text); }
.panel-quiet { background: transparent; }

/* ---- Tables --------------------------------------------------------- */
.table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    background: var(--surface);
    -webkit-overflow-scrolling: touch;
}
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 12px 14px; vertical-align: middle; }
thead th {
    position: sticky;
    top: 0;
    z-index: 2;
    background: var(--surface-2);
    color: var(--text-tertiary);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
    border-bottom: 1px solid var(--border-strong);
}
tbody tr { border-top: 1px solid var(--border); }
tbody tr:first-child { border-top: 0; }
tbody tr:hover { background: var(--surface-2); }
tbody tr.group-start { border-top: 1px solid var(--border-strong); }
.empty-cell { color: var(--text-tertiary); text-align: center; padding: 28px 14px; }
.empty { color: var(--text-secondary); font-size: 14px; }

/* ---- Rating badges -------------------------------------------------- */
.tag {
    display: inline-flex;
    align-items: center;
    padding: 3px 9px;
    border-radius: 6px;
    font-family: var(--font-mono);
    font-size: 11.5px;
    letter-spacing: 0.01em;
    white-space: nowrap;
    background: var(--surface-3);
    color: var(--text-secondary);
}
.tag-bull { background: rgba(74, 222, 128, 0.12); color: var(--bull); }
.tag-bear { background: rgba(248, 113, 113, 0.12); color: var(--bear); }
.tag-neutral { background: rgba(251, 191, 36, 0.12); color: var(--neutral); }
.tag-empty { background: transparent; color: var(--text-tertiary); padding-left: 0; }
.rating-arrow { color: var(--text-tertiary); margin: 0 6px; font-size: 12px; }
.action-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--text-tertiary);
}
.action-up { background: var(--bull); }
.action-down { background: var(--bear); }
.action-new { background: var(--accent); }
.action-flat { background: var(--text-tertiary); }

/* ---- Evidence ------------------------------------------------------- */
.evidence {
    display: inline-flex;
    align-items: center;
    font-family: var(--font-mono);
    font-size: 10.5px;
    letter-spacing: 0.04em;
    padding: 2px 7px;
    border-radius: 5px;
    white-space: nowrap;
}
.evidence-verified { background: rgba(74, 222, 128, 0.10); color: var(--bull); }
.evidence-public { background: var(--surface-3); color: var(--text-secondary); }
.evidence-link { display: inline-flex; align-items: center; gap: 3px; text-decoration: none; }
.evidence-link:hover { text-decoration: none; }
.evidence-link:hover .evidence { filter: brightness(1.25); }
.evidence-arrow { color: var(--text-tertiary); font-size: 10px; }
.evidence-note {
    border: 1px dashed var(--border-strong);
    border-radius: var(--radius-md);
    padding: 16px 18px;
    color: var(--text-secondary);
    font-size: 12.5px;
    line-height: 1.8;
    margin: 20px 0 0;
}
.evidence-boundary {
    margin-top: 16px;
    padding: 14px 16px;
    border-left: 2px solid var(--border-strong);
    color: var(--text-secondary);
    font-size: 12.5px;
    line-height: 1.8;
}

/* ---- Chips ---------------------------------------------------------- */
.chip-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 4px; }
.chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 6px 13px;
    color: var(--text-secondary);
    background: var(--surface-2);
    font-size: 13px;
    transition: border-color 0.2s var(--ease-out), color 0.2s var(--ease-out);
}
.chip:hover { border-color: var(--gold); color: var(--text); text-decoration: none; }
.chip img { width: 18px; height: 18px; border-radius: 4px; }
.ticker-chip {
    font-family: var(--font-mono);
    font-size: 12px;
    letter-spacing: 0.04em;
    padding: 5px 10px;
    border-radius: var(--radius-sm);
}

/* ---- Metrics -------------------------------------------------------- */
.metric-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin: 20px 0 26px;
}
.metric {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 16px 18px;
}
.metric b {
    display: block;
    font-family: var(--font-mono);
    font-size: 24px;
    font-weight: 500;
    line-height: 1.2;
    color: var(--text);
    font-variant-numeric: tabular-nums;
}
.metric span {
    display: block;
    margin-top: 5px;
    font-size: 11.5px;
    letter-spacing: 0.04em;
    color: var(--text-tertiary);
}

/* ---- Tone / reaction ------------------------------------------------ */
.tone {
    display: inline-flex;
    padding: 4px 10px;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 11px;
    white-space: nowrap;
}
.tone-bull { color: var(--bull); border-color: rgba(74, 222, 128, 0.3); }
.tone-bear { color: var(--bear); border-color: rgba(248, 113, 113, 0.3); }
.tone-mixed { color: var(--gold); border-color: rgba(201, 164, 92, 0.3); }
.reaction-aligned { color: var(--bull); }
.reaction-diverged { color: var(--bear); }
.reaction-pending, .reaction-mixed { color: var(--text-secondary); }

/* ---- Symbol cell / logos -------------------------------------------- */
.symbol-cell { display: inline-flex; align-items: center; gap: 9px; }
.symbol {
    font-family: var(--font-mono);
    font-size: 13px;
    letter-spacing: 0.03em;
    color: var(--text);
}
a.symbol:hover { color: var(--gold); text-decoration: none; }
.logo-wrap { position: relative; flex: 0 0 auto; }
.row-logo-wrap { width: 20px; height: 20px; }
.row-logo {
    width: 20px;
    height: 20px;
    border-radius: 5px;
    object-fit: contain;
    background: var(--surface-3);
    display: block;
}
.row-logo-fallback {
    display: none;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 5px;
    background: var(--surface-3);
    color: var(--text-tertiary);
    font-family: var(--font-mono);
    font-size: 10px;
}
.logo { width: 34px; height: 34px; border-radius: var(--radius-sm); vertical-align: middle; margin-right: 10px; }

/* ---- Misc ----------------------------------------------------------- */
.kv { display: grid; grid-template-columns: 168px 1fr; gap: 10px 18px; font-size: 14px; }
.kv div:nth-child(odd) { color: var(--text-secondary); }
.source-list { padding-left: 20px; margin: 0; }
.source-list li { margin: 0 0 8px; line-height: 1.75; }
.disclaimer { margin-top: 20px; font-size: 12px; color: var(--text-tertiary); line-height: 1.8; }

@media (max-width: 760px) {
    .wrap { padding: 26px 16px 72px; }
    .kv { grid-template-columns: 1fr; }
    .section { margin-bottom: 44px; }
    .section-head { flex-wrap: wrap; gap: 6px; }
    .panel { padding: 20px 18px; border-radius: var(--radius-md); }
    th, td { padding: 11px 12px; }
}
"""

PAGE_CSS = TOKENS_CSS + BASE_CSS + COMPONENTS_CSS

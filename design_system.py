"""FResearch 统一设计系统。

首页（`ib_research_server.py`）和 SEO/GEO 子页（`ib_research_geo.py`）以前各自维护
一份 token 和组件样式，同一个色板被抄了两遍、子页也拿不到主题切换。这里是单一源头：

- ``TOKENS_CSS``      —— 字体、四套主题的 CSS 变量、圆角与缓动
- ``BASE_CSS``        —— reset、页面底噪与光晕、``.wrap``、排版工具类
- ``COMPONENTS_CSS``  —— 导航、面包屑、面板、表格、徽章、chip、指标、页脚等公共组件
- ``MOTION_CSS``      —— 入场、滚动浮现、主题过渡、阅读进度等动效
- ``PAGE_CSS``        —— 以上拼接，任何整页都应该只引这一份
- ``MOTION_HEAD_SCRIPT`` / ``MOTION_SCRIPT`` —— 动效所需的最小脚本，分别放 <head> 与 </body> 前

首页在 ``PAGE_CSS`` 之后再追加自己的专属样式（hero、资产网格、工具栏等）。
"""

TOKENS_CSS = """
:root {
    color-scheme: dark;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
    --font-serif: "Iowan Old Style", "Songti SC", "STSong", Georgia, serif;
    --font-mono: "SF Mono", "SFMono-Regular", ui-monospace, "Cascadia Mono", Menlo, Consolas, "Liberation Mono", monospace;
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
    --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
    --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);
    --dur-fast: 0.18s;
    --dur-med: 0.35s;
    --dur-slow: 0.8s;
}
:root[data-theme="light"] {
    color-scheme: light;
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
    color-scheme: light;
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
    color-scheme: dark;
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
    will-change: transform;
}
::selection { background: var(--gold-glow); color: var(--text); }
html { scrollbar-color: var(--surface-3) transparent; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
    background: var(--surface-3);
    border: 3px solid var(--bg);
    border-radius: 999px;
}
::-webkit-scrollbar-thumb:hover { background: var(--border-strong); }
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
"""

MOTION_CSS = """
/* 动效分三层：首屏 .reveal 由 CSS 直接入场；首屏以下 .reveal-scroll / .stagger
   由 MOTION_SCRIPT 在进入视口时加 .is-visible。隐藏态只在 <html> 带 .js-motion
   且用户未要求减少动态时生效，脚本失效或爬虫抓取时内容始终可见。
   入场动画一律 backwards 填充：结束后交还给元素自身的 hover transform。 */
@keyframes fadeUp {
    from { opacity: 0; transform: translateY(22px); }
    to { opacity: 1; transform: translateY(0); }
}
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes lineRise { from { transform: translateY(105%); } to { transform: translateY(0); } }
@keyframes goldSweep {
    from { background-position: 100% 0; }
    to { background-position: 0 0; }
}
@keyframes barGrow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
@keyframes rowIn {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
}
@keyframes ambientDrift {
    from { transform: translate3d(0, 0, 0) scale(1); }
    to { transform: translate3d(14vw, 10vh, 0) scale(1.12); }
}
@keyframes scrollProgress { from { transform: scaleX(0); } to { transform: scaleX(1); } }

@media (prefers-reduced-motion: no-preference) {
    body::after { animation: ambientDrift 32s var(--ease-in-out) infinite alternate; }

    .reveal { animation: fadeUp var(--dur-slow) var(--ease-out) backwards; }
    .reveal-delay-1 { animation-delay: 0.06s; }
    .reveal-delay-2 { animation-delay: 0.14s; }
    .reveal-delay-3 { animation-delay: 0.22s; }
    .reveal-delay-4 { animation-delay: 0.30s; }
    .reveal-delay-5 { animation-delay: 0.38s; }

    .js-motion .reveal-scroll:not(.is-visible),
    .js-motion main > :nth-child(n+5):not(.is-visible) { opacity: 0; }
    .js-motion .reveal-scroll.is-visible,
    .js-motion main > :nth-child(n+5).is-visible {
        animation: fadeUp 0.9s var(--ease-out) backwards;
        animation-delay: var(--reveal-delay, 0s);
    }
    .js-motion main > :nth-child(-n+4) { animation: fadeUp var(--dur-slow) var(--ease-out) backwards; }
    .js-motion main > :nth-child(2) { animation-delay: 0.06s; }
    .js-motion main > :nth-child(3) { animation-delay: 0.12s; }
    .js-motion main > :nth-child(4) { animation-delay: 0.18s; }

    .js-motion .stagger:not(.is-visible) > * { opacity: 0; }
    .js-motion .stagger.is-visible > * {
        animation: fadeUp 0.7s var(--ease-out) backwards;
        animation-delay: calc(var(--stagger-i, 0) * 70ms + 0.08s);
    }

    /* 标题下的金线随区块入场从左侧画出 */
    .js-motion .reveal-scroll:not(.is-visible) .section-head::after { transform: scaleX(0); }
    .section-head::after { transition: transform 0.9s var(--ease-out) 0.25s; }

    /* 阅读进度：支持滚动时间线的浏览器才显示，不额外占用脚本 */
    @supports (animation-timeline: scroll()) {
        .site-nav::after {
            content: "";
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 100;
            height: 2px;
            background: linear-gradient(90deg, var(--gold-dim), var(--gold) 60%, var(--gold-bright));
            transform-origin: 0 50%;
            pointer-events: none;
            animation: scrollProgress linear both;
            animation-timeline: scroll(root block);
        }
    }
}

/* 主题切换期间临时给颜色加过渡，平时不挂，避免拖慢普通交互 */
html.theme-switching body,
html.theme-switching body *,
html.theme-switching body *::before,
html.theme-switching body *::after {
    transition:
        background-color 0.45s var(--ease-out),
        color 0.45s var(--ease-out),
        border-color 0.45s var(--ease-out),
        box-shadow 0.45s var(--ease-out),
        transform 0.45s var(--ease-spring) !important;
}

@media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
    }
}
@media print {
    .reveal, .reveal-scroll, .stagger > *, main > * { opacity: 1 !important; animation: none !important; }
    body::before, body::after { display: none; }
}
"""

COMPONENTS_CSS = """
/* ---- Site chrome ---------------------------------------------------- */
.site-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 22px;
    align-items: center;
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
    text-decoration: none;
}
.site-nav .brand::after { content: "."; color: var(--gold); }
.site-nav a {
    color: var(--text-secondary);
    font-size: 13.5px;
    text-decoration: none;
    transition: color var(--dur-fast) var(--ease-out);
}
.site-nav a:not(.brand) { position: relative; padding: 4px 0; }
.site-nav a:not(.brand)::before {
    content: "";
    position: absolute;
    left: 0;
    right: 0;
    bottom: -1px;
    height: 1px;
    background: var(--gold);
    transform: scaleX(0);
    transform-origin: 100% 50%;
    transition: transform var(--dur-med) var(--ease-out);
}
.site-nav a:hover { color: var(--gold); text-decoration: none; }
.site-nav a[aria-current="page"] { color: var(--gold); }
.site-nav a:not(.brand):hover::before,
.site-nav a[aria-current="page"]::before { transform: scaleX(1); transform-origin: 0 50%; }
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
.footer-nav a { color: var(--text-secondary); transition: color var(--dur-fast) var(--ease-out); }
.footer-nav a:hover { color: var(--gold); text-decoration: none; }
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
.theme-btn svg { width: 15px; height: 15px; transition: transform var(--dur-med) var(--ease-spring); }
.theme-btn:hover { color: var(--text); }
.theme-btn:hover svg { transform: rotate(-12deg) scale(1.08); }
.theme-btn.active { background: var(--gold); color: var(--bg); }
/* 选中态是一颗在三个按钮间滑动的金色胶囊；不支持 :has 的浏览器保留按钮自身底色 */
@supports selector(:has(*)) {
    .theme-switcher { position: relative; isolation: isolate; }
    .theme-switcher::before {
        content: "";
        position: absolute;
        top: 3px;
        left: 3px;
        z-index: -1;
        width: 30px;
        height: 30px;
        border-radius: 999px;
        background: var(--gold);
        box-shadow: 0 2px 10px var(--gold-glow);
        transition: transform 0.5s var(--ease-spring);
    }
    .theme-switcher:has(.theme-btn[data-theme="light"].active)::before { transform: translateX(32px); }
    .theme-switcher:has(.theme-btn[data-theme="sepia"].active)::before { transform: translateX(64px); }
    .theme-switcher .theme-btn.active { background: transparent; }
}

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
    position: relative;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 18px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
}
.section-head::after {
    content: "";
    position: absolute;
    bottom: -1px;
    left: 0;
    width: 72px;
    height: 1px;
    background: var(--gold);
    opacity: 0.75;
    transform-origin: 0 50%;
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
tbody tr { border-top: 1px solid var(--border); transition: background-color var(--dur-fast) var(--ease-out); }
tbody tr:first-child { border-top: 0; }
tbody tr:hover { background: var(--surface-2); }
tbody td:first-child { transition: box-shadow var(--dur-fast) var(--ease-out); }
tbody tr:hover td:first-child { box-shadow: inset 2px 0 0 var(--gold); }
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
    transition:
        border-color var(--dur-fast) var(--ease-out),
        color var(--dur-fast) var(--ease-out),
        transform var(--dur-med) var(--ease-spring),
        background-color var(--dur-fast) var(--ease-out);
}
.chip:hover {
    border-color: var(--gold);
    color: var(--text);
    background: var(--gold-dim);
    text-decoration: none;
    transform: translateY(-1px);
}
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
    transition: border-color var(--dur-med) var(--ease-out), transform var(--dur-med) var(--ease-out);
}
.metric:hover { border-color: var(--border-strong); transform: translateY(-2px); }
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
.symbol-cell { display: inline-flex; align-items: center; gap: 9px; min-width: 0; }
.symbol {
    font-family: var(--font-mono);
    font-size: 13px;
    letter-spacing: 0.03em;
    color: var(--text);
}
a.symbol:hover { color: var(--gold); text-decoration: none; }
.logo-wrap {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    overflow: hidden;
}
.row-logo-wrap {
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);
}
.row-logo {
    width: 100%;
    height: 100%;
    padding: 3px;
    object-fit: contain;
    display: block;
    transition: transform var(--dur-med) var(--ease-spring);
}
tr:hover .row-logo,
.symbol-cell:hover .row-logo,
.asset-chip:hover .row-logo { transform: scale(1.08); }
.row-logo-fallback {
    display: none;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    background: var(--gold-dim);
    color: var(--gold);
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
}
.title-with-logo { display: flex; align-items: center; gap: 12px; }
.title-with-logo .row-logo-wrap { width: 42px; height: 42px; border-radius: 12px; }
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

PAGE_CSS = TOKENS_CSS + BASE_CSS + COMPONENTS_CSS + MOTION_CSS

# 必须在首屏渲染前打上 .js-motion，否则首屏以下的区块会先闪现再隐藏。
# load 时若 MOTION_SCRIPT 没跑起来（被拦截或报错），撤掉标记，内容照常显示。
MOTION_HEAD_SCRIPT = """<script>(function(d){d.documentElement.classList.add('js-motion');\
addEventListener('load',function(){if(!window.__fresearchMotion){d.documentElement.classList.remove('js-motion');}});})(document);</script>"""

MOTION_SCRIPT = """<script>
(function(){
  window.__fresearchMotion = true;
  var root = document.documentElement;
  document.querySelectorAll('.stagger').forEach(function(group){
    Array.prototype.forEach.call(group.children, function(child, i){
      child.style.setProperty('--stagger-i', String(Math.min(i, 11)));
    });
  });
  var targets = document.querySelectorAll('.reveal-scroll, .stagger, main > :nth-child(n+5)');
  function show(el){ el.classList.add('is-visible'); }
  if (!('IntersectionObserver' in window)) {
    targets.forEach(show);
  } else {
    var io = new IntersectionObserver(function(entries){
      var batch = 0;
      entries.forEach(function(entry){
        if (!entry.isIntersecting) return;
        entry.target.style.setProperty('--reveal-delay', (Math.min(batch, 4) * 0.08) + 's');
        batch += 1;
        show(entry.target);
        io.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0 });
    targets.forEach(function(el){ io.observe(el); });
  }
  document.addEventListener('click', function(e){
    if (!e.target.closest || !e.target.closest('.theme-btn')) return;
    root.classList.add('theme-switching');
    clearTimeout(window.__fresearchThemeTimer);
    window.__fresearchThemeTimer = setTimeout(function(){ root.classList.remove('theme-switching'); }, 520);
  }, true);
})();
</script>"""

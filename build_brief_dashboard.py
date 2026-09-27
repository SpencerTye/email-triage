"""
build_brief_dashboard.py

Renders every YYYY-MM-DD.md brief in a folder into one static HTML page
(index.html in the same folder), with a date list on the left. The newest
brief opens by default.

Usage:
  python build_brief_dashboard.py --briefs-dir PATH

Needs the `markdown` package (pip install markdown).
"""

import argparse
import sys
import re
from pathlib import Path

try:
    import markdown
except ImportError:
    print("[FAIL] 'markdown' package is not installed.", flush=True)
    print("       Install it with: python -m pip install markdown", flush=True)
    sys.exit(2)

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def collect_briefs(briefs_dir):
    """Scan Briefs dir for YYYY-MM-DD.md files. Return list[(date_str, text)] newest-first."""
    briefs = []
    for p in sorted(briefs_dir.glob("*.md")):
        m = DATE_RE.match(p.name)
        if not m:
            continue
        date = m.group(1)
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            text = f"(read error: {e})"
        briefs.append((date, text))
    briefs.sort(key=lambda x: x[0], reverse=True)
    return briefs


def render_md(text):
    # md_in_html lets any markdown inside our raw <div class="item"> blocks
    # still render; without it, block-level HTML is passed through as-is,
    # which is what compose_brief.py emits anyway.
    return markdown.markdown(text, extensions=["extra", "sane_lists"])


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Morning briefs</title>
<style>
  body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background: #f5f5f7; color: #1a1a1a; }
  #layout { display: flex; height: 100vh; }
  #sidebar { width: 220px; background: #222; color: #ddd; overflow-y: auto; padding: 12px 0; flex-shrink: 0; }
  #sidebar h2 { margin: 0 16px 12px; font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }
  .date-link { display: block; padding: 8px 16px; color: #ddd; text-decoration: none; border-left: 3px solid transparent; cursor: pointer; font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
  .date-link:hover { background: #2c2c2c; }
  .date-link.active { background: #333; border-left-color: #4a9eff; color: #fff; }

  #content { flex: 1; overflow-y: auto; padding: 28px 40px; }
  #content-inner { max-width: 820px; }
  #content h1 { margin: 0 0 4px; font-size: 22px; }
  #content h2 { margin: 26px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #e3e3e6; font-size: 16px; color: #333; font-weight: 600; letter-spacing: 0.2px; }
  #content p { margin: 4px 0; color: #555; font-size: 13px; }
  #content code { background: #eceef5; padding: 1px 5px; border-radius: 3px; font-size: 0.85em; font-family: ui-monospace, Consolas, monospace; color: #334; }
  #content pre { background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }
  #content blockquote { border-left: 3px solid #ccc; margin: 0; padding-left: 16px; color: #555; }

  /* Per-item cards emitted by compose_brief.py */
  .item { padding: 10px 14px; margin: 0 0 8px; background: #fff; border: 1px solid #e3e3e6; border-left: 3px solid #4a9eff; border-radius: 3px; box-shadow: 0 1px 2px rgba(0,0,0,0.03); }
  .item-anomaly { border-left-color: #d04040; }
  .item-subject { font-weight: 600; font-size: 14.5px; margin-bottom: 3px; color: #111; line-height: 1.3; }
  .item-summary { color: #333; margin-bottom: 5px; font-size: 13.5px; line-height: 1.45; }
  .item-meta { font-size: 11px; color: #888; font-family: ui-monospace, Consolas, monospace; }
  .item-reason { margin-top: 6px; font-size: 12px; color: #a04040; font-style: italic; }

  /* Fallback: older briefs that still use bulleted item lines */
  #content ul { padding-left: 18px; margin: 6px 0; }
  #content ul li { margin-bottom: 4px; font-size: 13.5px; line-height: 1.45; }
  #content ol { padding-left: 22px; }

  /* Stats / Errors sections use bulleted lists; keep them compact */
  #content h2 + ul { background: #fff; border: 1px solid #e3e3e6; border-radius: 3px; padding: 10px 14px 10px 30px; }

  .brief { display: none; }
  .brief.active { display: block; }
  .empty { color: #888; font-style: italic; padding: 16px; }
  footer { margin-top: 40px; color: #aaa; font-size: 11px; border-top: 1px solid #e3e3e6; padding-top: 10px; }
</style>
</head>
<body>
<div id="layout">
  <nav id="sidebar">
    <h2>Briefs</h2>
    {{SIDEBAR}}
  </nav>
  <main id="content"><div id="content-inner">
    {{CONTENT}}
    <footer>Static dashboard. Regenerated each morning and on demand via BATs\build_brief_dashboard.bat.</footer>
  </div></main>
</div>
<script>
  function show(date) {
    document.querySelectorAll('.brief').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.date-link').forEach(el => el.classList.remove('active'));
    const target = document.getElementById('brief-' + date);
    const link = document.getElementById('link-' + date);
    if (target) target.classList.add('active');
    if (link) link.classList.add('active');
  }
  document.querySelectorAll('.date-link').forEach(el => {
    el.addEventListener('click', e => { e.preventDefault(); show(el.dataset.date); });
  });
</script>
</body>
</html>
"""


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Render the briefs folder as one HTML page."
    )
    p.add_argument(
        "--briefs-dir",
        type=Path,
        required=True,
        help="folder holding YYYY-MM-DD.md briefs",
    )
    args = p.parse_args(argv)
    briefs_dir = args.briefs_dir
    out_file = briefs_dir / "index.html"
    if not briefs_dir.exists():
        print(f"[FAIL] {briefs_dir} not found", flush=True)
        return 1

    briefs = collect_briefs(briefs_dir)

    if not briefs:
        sidebar = '<div class="empty">No briefs yet.</div>'
        content = '<div class="empty">No YYYY-MM-DD.md briefs found.</div>'
    else:
        sidebar_items = []
        content_items = []
        for i, (date, text) in enumerate(briefs):
            active_cls = " active" if i == 0 else ""
            sidebar_items.append(
                f'<a class="date-link{active_cls}" id="link-{date}" data-date="{date}" href="#">{date}</a>'
            )
            rendered = render_md(text)
            content_items.append(
                f'<div class="brief{active_cls}" id="brief-{date}">{rendered}</div>'
            )
        sidebar = "\n    ".join(sidebar_items)
        content = "\n    ".join(content_items)

    html_out = TEMPLATE.replace("{{SIDEBAR}}", sidebar).replace("{{CONTENT}}", content)
    out_file.write_text(html_out, encoding="utf-8")
    print(f"[OK] Rendered {len(briefs)} brief(s) -> {out_file}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

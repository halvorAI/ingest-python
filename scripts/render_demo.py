"""Runs `halvorai-ingest demo` and renders its real output as an animated terminal SVG (assets/demo.svg),
so the README demo never drifts from what the command prints. Run: uv run python scripts/render_demo.py"""

from __future__ import annotations

import re
import subprocess
import sys
from html import escape
from pathlib import Path

root = Path(__file__).resolve().parent.parent
out = subprocess.run([sys.executable, "-m", "halvorai_ingest", "demo"], cwd=root, check=True, capture_output=True, text=True).stdout
out = re.sub(r"evt_[0-9A-Z]{26}(?=: Travel)", "evt_01KDEMO0000000000000000000", out)
lines = ["$ uvx --from halvorai-ingest halvorai-ingest demo", *out.rstrip().split("\n")]


def cls(t: str) -> str:
    if t.startswith("$"):
        return "p"
    if "FAILED" in t:
        return "bad"
    if re.match(r"^\d\.", t):
        return "hd"
    return "v" if t.endswith("verified") else "n"


lh, top, w = 20, 52, 860
h = top + len(lines) * lh + 20
rows = "\n".join(
    f'<text class="{cls(t)}" x="20" y="{top + i * lh}" style="animation-delay:{i * 0.25:.2f}s">{escape(t)}</text>'
    for i, t in enumerate(lines)
)
svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-labelledby="t">
<title id="t">Output of halvorai-ingest demo: an event is signed and verified, then the same event with a changed amount fails verification</title>
<style>
text{{font:13px 'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;opacity:0;animation:in .01s forwards}}
@keyframes in{{to{{opacity:1}}}}
@media (prefers-reduced-motion:reduce){{text{{animation:none;opacity:1}}}}
.n{{fill:#F3EFE6}}.p{{fill:#A39E91}}.hd{{fill:#F3EFE6;font-weight:700}}.v{{fill:#C6F432}}.bad{{fill:#F0624D}}
</style>
<rect width="{w}" height="{h}" rx="10" fill="#1C1C1C"/>
<circle cx="22" cy="20" r="6" fill="#3A3A36"/><circle cx="42" cy="20" r="6" fill="#3A3A36"/><circle cx="62" cy="20" r="6" fill="#3A3A36"/>
{rows}
</svg>
"""
(root / "assets" / "demo.svg").write_text(svg, encoding="utf-8")
print(f"wrote assets/demo.svg ({len(lines)} lines)")

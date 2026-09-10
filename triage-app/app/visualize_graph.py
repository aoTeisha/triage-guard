"""Open the control-plane graph as an interactive Mermaid diagram in the browser.

Complements `plot()` (app/main.py), which writes the static .mmd file used in
docs. This is for a quick local look — nothing here is committed or read back.
"""

from __future__ import annotations

import re
import tempfile
import webbrowser

from app.graph import build_graph

# LangGraph labels conditional edges with str(enum_member), e.g. "Route.ESCALATE".
# The bare "." breaks mermaid's flowchart grammar (confirmed against mermaid-cli
# and the mermaid.ink render API, not just one CDN version) — strip it here rather
# than in the transition table, since this is a rendering-only concern.
_ENUM_LABEL = re.compile(r"\b([A-Z][A-Za-z]+)\.([A-Z_]+)\b")

_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Triage Guard — Control Plane</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
</head>
<body>
  <div class="mermaid">{mermaid}</div>
  <script>mermaid.initialize({{startOnLoad: true, theme: 'default'}});</script>
</body>
</html>"""


def visualize() -> None:
    mermaid = build_graph().get_graph().draw_mermaid()
    mermaid = _ENUM_LABEL.sub(r"\1_\2", mermaid)
    html = _HTML.format(mermaid=mermaid)

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
        path = f.name

    webbrowser.open(f"file://{path}")
    print(f"Graph opened in browser: {path}")


if __name__ == "__main__":
    visualize()

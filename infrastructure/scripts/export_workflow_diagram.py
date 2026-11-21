"""Regenerates docs/diagrams/research_graph.mmd from the real compiled LangGraph (run: python infrastructure/scripts/export_workflow_diagram.py)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.deps import AgentDeps  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.workflows.research_graph import build_research_graph  # noqa: E402

graph = build_research_graph(AgentDeps(None, None, None, None, None, Settings(env="test")))
out = ROOT / "docs" / "diagrams" / "research_graph.mmd"
out.write_text(graph.get_graph().draw_mermaid())
print(f"wrote {out}")

"""docs/for-agents.md and llms.txt must only name real MCP tools, and llms.txt must list them all."""
import re
from pathlib import Path

from rainout_agent import mcp_server

ROOT = Path(__file__).resolve().parents[1]
TOOLS = set(mcp_server.HANDLERS)


def test_llms_txt_lists_every_tool():
    line = next(l for l in (ROOT / "llms.txt").read_text().splitlines() if l.startswith("- MCP (Streamable HTTP"))
    listed = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", line.split("Tools:", 1)[1].split(". All")[0]))
    assert listed == TOOLS


def test_for_agents_names_real_tools_and_routes():
    text = (ROOT / "docs/for-agents.md").read_text()
    named = {t for t in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text) if t in TOOLS or (t.startswith(("check_", "get_", "find_")) and t != "check_again_at")}
    assert named <= TOOLS and {"should_we_cancel", "go_no_go", "check_game"} <= named
    from rainout_agent.construction_api import ROUTES

    assert "/v1/should-we-cancel" in ROUTES and "/v1/should-we-cancel" in (ROOT / "llms.txt").read_text()

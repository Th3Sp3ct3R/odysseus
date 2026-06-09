"""
bus_task_server.py

Built-in MCP server giving the Odysseus agent the ability to hand work to
Hermes (the executor) over the VANTA-Brain bus. This is the "orchestrator can
assign autonomously" piece — the agent calls `bus_task(action="emit", ...)`
during planning and a task spec lands in tasks/inbox/ for Hermes to claim.

Exposes one tool, `bus_task`:
  - emit : write a task to the bus inbox (goal + acceptance + priority)
  - list : show inbox/active/done counts

Wraps src/bus_tasks.py (single source of truth). PYTHONPATH is set to the repo
root by builtin_mcp.register_builtin_servers.
"""

import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("bus_task")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="bus_task",
            description=(
                "Hand a task to Hermes (the executor) via the shared bus, or check bus status. "
                "Use action='emit' to assign a build/work task; it appears in Hermes's queue and "
                "Hermes claims, executes, and reports results back into memory. action='list' shows counts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["emit", "list"],
                        "description": "emit a task to Hermes, or list bus task counts",
                    },
                    "goal": {"type": "string", "description": "One-line objective (required for emit)"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Task priority (emit)",
                    },
                    "acceptance": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Acceptance criteria — how Hermes knows it's done (emit)",
                    },
                    "body": {"type": "string", "description": "Longer details/context for the task (emit)"},
                },
                "required": ["action"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "bus_task":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    action = arguments.get("action", "")
    try:
        from src.bus_tasks import emit_task, _counts
    except Exception as e:
        return [TextContent(type="text", text=f"Error: bus_tasks unavailable ({type(e).__name__}: {e})")]

    if action == "emit":
        goal = (arguments.get("goal") or "").strip()
        if not goal:
            return [TextContent(type="text", text="Error: emit needs a 'goal'.")]
        try:
            r = emit_task(
                goal,
                acceptance=arguments.get("acceptance") or None,
                priority=arguments.get("priority") or "normal",
                body=arguments.get("body") or "",
            )
            return [TextContent(
                type="text",
                text=f"✅ Task handed to Hermes: \"{goal}\" (id {r['id'][:8]}). "
                     f"It is now in the executor's queue ({r['file']}).",
            )]
        except Exception as e:
            return [TextContent(type="text", text=f"Error emitting task: {type(e).__name__}: {e}")]

    elif action == "list":
        c = _counts()
        return [TextContent(
            type="text",
            text=f"Bus tasks — inbox(waiting): {c['inbox']}, active(in progress): {c['active']}, done: {c['done']}",
        )]

    return [TextContent(type="text", text=f"Error: unknown action '{action}'. Use: emit, list")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

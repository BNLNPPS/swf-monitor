# Model Context Protocol (MCP) Integration

SWF Monitor exposes a Model Context Protocol (MCP) server for LLM access to
testbed state, PCS, epicprod, PanDA monitoring, and selected control actions.

| Item | Value |
|---|---|
| Endpoint | `/swf-monitor/mcp/` |
| Local URL | `http://127.0.0.1:8001/swf-monitor/mcp/` |
| Server | FastMCP from the official `mcp` Python SDK |
| ASGI entrypoint | `src/swf_monitor_project/mcp_asgi.py` |
| Tool package | `src/monitor_app/mcp/` |

## Related Documentation

| Document | Contents |
|---|---|
| [MCP client setup](MCP_CLIENTS.md) | Claude Code and Claude Desktop configuration, bearer-token and OAuth notes, full settings example. |
| [MCP tool reference](MCP_TOOL_REFERENCE.md) | Complete tool catalog, parameters, return fields, usage notes, and example prompts. |
| [PanDA Mattermost bot](PANDA_BOT.md) | Bot architecture, tool loading, external MCP servers, environment variables, and transport behavior. |
| [Production deployment](PRODUCTION_DEPLOYMENT.md) | ASGI worker deployment, Apache proxying, watchdog, and service operations. |

## Operating Model

MCP is operated on swf-testbed as stateless POST request/response MCP over HTTP.
The useful tool surface is `initialize`, `tools/list`, and `tools/call`
returning JSON responses. Local clients use the loopback ASGI endpoint. The
public Apache path still exists, but remote Claude.ai GET/SSE streaming is not
an operational dependency of the current deployment.

The ASGI worker runs separately from the mod_wsgi Django site. This isolates MCP
failures from the browser UI and REST API. `MCPRequestGuard` in
`mcp_asgi.py` handles the transport and bearer-token gate: `/health` is open for
the watchdog; other requests require POST plus `Authorization: Bearer
<MCP_BEARER_TOKEN>`.

OAuth 2.1/Auth0 wiring remains available for a future remote-access mode. Leave
`AUTH0_DOMAIN` empty to disable OAuth. Remote MCP access must not be exposed
without OAuth and a fresh transport-lifecycle validation.

## Architecture Notes

- `monitor_app.mcp.__init__` creates the shared `FastMCP` instance. Importing
  the `monitor_app.mcp` package registers each decorated tool on that instance.
- FastMCP owns tool registration and the streamable-HTTP app
  (`mcp.streamable_http_app()`). `django-mcp-server` is no longer used.
- `mcp_asgi.py` owns `mcp.session_manager.run()` for the ASGI process lifetime,
  avoiding the per-request session-manager lifecycle that caused risk in the old
  adapter path.
- Starlette is used only as the ASGI host for FastMCP. It is present through the
  `mcp` dependency, not as an application framework for swf-monitor.
- `starlette>=1.2.1` is pinned to clear CVE-2026-48710. The request guard also
  avoids the affected `request.url` path decision pattern: it checks the raw
  ASGI path and bearer token.
- Long-lived GET/SSE streaming is not used on swf-testbed. If it becomes needed,
  implement it deliberately with a lifespan-managed
  `StreamableHTTPSessionManager`.

## Design

MCP tools are data access primitives with filtering. The LLM composes multiple
tool calls when a question needs synthesis or aggregation.

List tools follow common conventions:

- Time filters are ISO datetime strings named `start_time` and `end_time`.
- Omitted time ranges default to a reasonable recent period for that tool.
- Paginated list responses include `items`, `total_count`, `has_more`, and
  `monitor_urls` where applicable.

Tool docstrings are operational metadata. They are the primary text an LLM sees
when deciding which tool to call and how to call it.

## Tool Categories

The full tool catalog is in [MCP tool reference](MCP_TOOL_REFERENCE.md).

| Category | Tools | Count |
|---|---|---:|
| Tool Discovery | `swf_list_available_tools`, `get_server_instructions` | 2 |
| System State | `swf_get_system_state` | 1 |
| Agents | `swf_list_agents`, `swf_get_agent` | 2 |
| Namespaces | `swf_list_namespaces`, `swf_get_namespace` | 2 |
| Workflow Definitions | `swf_list_workflow_definitions` | 1 |
| Workflow Executions | `swf_list_workflow_executions`, `swf_get_workflow_execution` | 2 |
| Messages | `swf_list_messages`, `swf_send_message` | 2 |
| Runs | `swf_list_runs`, `swf_get_run` | 2 |
| STF Files | `swf_list_stf_files`, `swf_get_stf_file` | 2 |
| TF Slices | `swf_list_tf_slices`, `swf_get_tf_slice` | 2 |
| Logs | `swf_list_logs`, `swf_get_log_entry` | 2 |
| Workflow Control | `swf_start_workflow`, `swf_stop_workflow`, `swf_end_execution` | 3 |
| Agent Management | `swf_kill_agent` | 1 |
| User Agent Manager | `swf_check_agent_manager`, `swf_get_testbed_status`, `swf_start_user_testbed`, `swf_stop_user_testbed` | 4 |
| Workflow Monitoring | `swf_get_workflow_monitor`, `swf_list_workflow_monitors` | 2 |
| AI Memory | `swf_record_ai_memory`, `swf_get_ai_memory` | 2 |
| AI Content | `epic_register_ai_assessment`, `epic_get_ai_content` | 2 |
| AI Proposals | `ai_list_proposals`, `ai_decide_proposal` | 2 |
| Action Stream | `epicprod_list_actions` | 1 |
| PCS Tags | `pcs_list_tags`, `pcs_get_tag`, `pcs_search_tags` | 3 |
| PCS Datasets and Prod Tasks | `pcs_dataset_list`, `pcs_dataset_get`, `pcs_dataset_intake`, `pcs_prodtask_list`, `pcs_prodtask_get`, `pcs_prodtask_artifact`, `pcs_prodtask_intake`, `pcs_prodtask_link_input`, `pcs_prodtask_set_status` | 9 |
| PanDA Production | `panda_get_activity`, `panda_list_jobs`, `panda_diagnose_jobs`, `panda_list_tasks`, `panda_error_summary`, `panda_study_job`, `panda_list_queues`, `panda_get_queue`, `panda_resource_usage`, `panda_harvester_workers` | 10 |
| **Total** | | **59** |

## Implementation Files

| File | Purpose |
|---|---|
| `src/swf_monitor_project/mcp_asgi.py` | Starlette ASGI app hosting FastMCP, with `MCPRequestGuard` and `/health`. |
| `src/monitor_app/mcp/__init__.py` | Shared `FastMCP` instance and package-level tool registration. |
| `src/monitor_app/mcp/common.py` | Discovery helpers, including `swf_list_available_tools`. |
| `src/monitor_app/mcp/system.py` | System, agent, namespace, workflow-control, and monitoring tools. |
| `src/monitor_app/mcp/workflows.py` | Workflow, message, run, STF, TF-slice, and log tools. |
| `src/monitor_app/mcp/ai_memory.py` | AI memory tools. |
| `src/monitor_app/mcp/ai_content.py` | epicprod AI-assessment tools. |
| `src/monitor_app/mcp/ai_proposals.py` | AI proposal listing and human-decision relay (the bot review flow). |
| `src/monitor_app/mcp/pandamon.py` | PanDA production monitoring tools. |
| `src/monitor_app/auth0.py` | JWT validation with Auth0 JWKS. |
| `src/monitor_app/middleware.py` | OAuth-related MCP authentication middleware. |
| `src/swf_monitor_project/settings.py` | MCP server name, instructions, bearer token, and optional Auth0 settings. |

## Settings

```python
MCP_SERVER_NAME = "swf-testbed"
MCP_SERVER_INSTRUCTIONS = """..."""
MCP_BEARER_TOKEN = config("MCP_BEARER_TOKEN", default="")

AUTH0_DOMAIN = config("AUTH0_DOMAIN", default="")
AUTH0_CLIENT_ID = config("AUTH0_CLIENT_ID", default="")
AUTH0_CLIENT_SECRET = config("AUTH0_CLIENT_SECRET", default="")
AUTH0_API_IDENTIFIER = config("AUTH0_API_IDENTIFIER", default="")
AUTH0_ALGORITHMS = ["RS256"]
```

`MCP_SERVER_NAME` is hardcoded by clients and permission strings. Do not rename
it without updating client configuration and permissions.

## Adding New Tools

Tools are defined with `@mcp.tool()` in `src/monitor_app/mcp/`. Importing the
package registers every decorated tool on the shared FastMCP instance.

```python
from asgiref.sync import sync_to_async
from monitor_app.mcp import mcp

@mcp.tool()
async def my_new_tool(param: str, start_time: str = None, end_time: str = None) -> dict:
    """
    Tool description shown to the LLM.

    Args:
        param: Parameter description.
        start_time: Optional ISO datetime for range start.
        end_time: Optional ISO datetime for range end.

    Returns:
        Result description.
    """
    @sync_to_async
    def do_work():
        return {"field": param}

    return await do_work()
```

After adding a new `@mcp.tool()`:

1. Add it to the hardcoded list in `swf_list_available_tools()` in
   `src/monitor_app/mcp/common.py`.
2. Update `MCP_SERVER_INSTRUCTIONS` in `src/swf_monitor_project/settings.py`.
3. Update [MCP tool reference](MCP_TOOL_REFERENCE.md).
4. Update the tool-category table in this file if the tool count or category
   membership changes.

## References

- [MCP specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [CVE-2026-48710 "BadHost"](https://github.com/advisories/GHSA-86qp-5c8j-p5mr)
- [OAuth2 Provider](https://django-oauth-toolkit.readthedocs.io/)

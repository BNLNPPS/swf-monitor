# PanDA Mattermost Bot

The PanDA Mattermost bot is an MCP client for production monitoring questions.
The SWF Monitor MCP server overview is [MCP.md](MCP.md), client setup is
[MCP_CLIENTS.md](MCP_CLIENTS.md), and the tool catalog is
[MCP_TOOL_REFERENCE.md](MCP_TOOL_REFERENCE.md).

The PanDA bot (`monitor_app/panda/bot.py`) is an MCP **client**. It answers production-monitoring questions in Mattermost by selecting and calling tools across multiple MCP servers.

**Architecture:**
- Listens on a Mattermost channel via WebSocket (`mattermostdriver`)
- Holds connections to the local swf-monitor MCP (HTTP — the `swf_*`, `pcs_*`, `panda_*` tools) plus stdio-launched external servers: **LXR** (EIC code browser cross-reference), **uproot** (ROOT file analysis), **GitHub**, **Zenodo**, **XRootD**, **JLab-Rucio**, **BNL-Rucio**, and **corun/codoc**
- The corun MCP server is a standalone stdio server that calls prod corun over REST (`CORUN_BASE_URL + /api/v1/...`). It queues generation jobs asynchronously; pandabot exposes submit/generate/status/page tools but deliberately does not expose the long-polling `wait_for_job` tool, so a long corun generation does not hold pandabot's single response lock. Tool surface and config detail: [corun-mcp-server](https://github.com/eic/corun-mcp-server) README.
- On startup, if `CORUN_API_TOKEN` is configured, pandabot ensures a corun notification subscription exists. Corun sends terminal job callbacks to `/swf-monitor/api/corun-callback/`; swf-monitor posts simple completion/failure/cancel notices to the fixed `#pandabot` Mattermost channel. There is no pandabot-side polling tracker. Callback payload schema: corun-ai `docs/job-system.md` § Job Notifications.
- Registers in-process **epicdoc** tools (`epic_doc_search`, `epic_doc_contents`) backed by a ChromaDB vector store of ePIC docs — runs inside the bot process, not as a separate MCP server
- **Bamboo** log analysis is used via the `panda_study_job` and `panda_harvester_workers` swf-monitor MCP tools, not as a separate MCP server
- For JLab Rucio campaign dataset queries, the bot should search the `epic` scope first (for example `scope="epic", name="*26.04.1*", type="DATASET"`) and must not infer absence from a single empty scope or from an XRootD permission error
- For Rucio dataset placement questions, replication rules are authoritative for managed RSE placement; replica/PFN listings may include transient staging endpoints and should not be summarized as persistent placement without checking rules
- System prompt is externalized to a file and re-read per message, so prompt iteration doesn't require a bot restart
- **3-tier tool awareness**: every tool is visible by name+one-liner in the system prompt so the LLM knows the full catalog; detailed schemas are fetched only for tools the LLM explicitly selects via `select_tools`; the bot preserves server and suggestion context across thread turns so follow-ups don't re-select from scratch
- **Progressive tool loading via semantic similarity**: for each user question the bot embeds the question and ranks tools by server-prefixed cosine similarity, auto-truncating at a score cliff — the LLM sees a small, relevant set rather than all hundreds of tools
- **DPID (Data Provenance ID) anti-fabrication**: for questions about specific jobs/tasks, the bot verifies the LLM cited a real DPID from tool output, strips the DPID from the user-facing reply, and warns if verification fails
- Bot-created AI assessments are stamped before the MCP call: `username` is `bot`, `ai` is the exact Claude model used for the message, and `data.origin` includes `type: "bot"`, `client: "mattermost"`, `harness: "bot"`, and the model
- Remembers recent Q&A exchanges (via `swf_record_ai_memory`) to improve responses over time. Memory is collective — the bot does not track or remember who asked what
- `/panda` slash commands for direct queries without LLM involvement (status, errors, jobs/tasks by filter, site detail)
- Server-side matplotlib plots rendered in Mattermost

**Running:** `manage.py panda_bot`

**Environment variables:**
- `MATTERMOST_URL` (default: `chat.epic-eic.org`)
- `MATTERMOST_TOKEN` (required)
- `MATTERMOST_TEAM` (default: `main`)
- `MATTERMOST_CHANNEL` (default: `pandabot`)
- `MCP_URL` (default: `http://127.0.0.1:8001/swf-monitor/mcp/`)
- `ANTHROPIC_API_KEY` (required, used by the Anthropic SDK)
- `CORUN_BASE_URL` (optional, default: `https://epic-devcloud.org/doc`)
- `CORUN_API_TOKEN` (optional; when set, enables the corun MCP server)
- `CORUN_CALLBACK_URL` (optional, default: `https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/corun-callback/`)
- `CORUN_SUBSCRIPTION_NAME` (optional, default: `pandabot-swf-testbed`)

**MCP transport:** The bot uses a minimal HTTP POST client (`MCPClient`) that sends JSON-RPC requests to the local MCP endpoint. Each user question gets a fresh stateless request/response exchange.

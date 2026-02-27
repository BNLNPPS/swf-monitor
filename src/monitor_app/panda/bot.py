"""
PanDA Mattermost bot — answers production monitoring questions using Claude with tool use.

Connects to Mattermost via WebSocket, listens for messages in a target channel,
and responds using Claude with PanDA monitoring tools discovered via MCP.

MCP transport: HTTP POST (JSON-RPC) to the Django MCP endpoint — the same
transport Claude Code uses. No SSE, no GET streams, no subprocesses.
"""

import json
import logging
import os

import anthropic
import httpx
from mattermostdriver import Driver

logger = logging.getLogger('panda_bot')

MAX_TOOL_ROUNDS = 10
MAX_RESULT_LEN = 10000
MM_POST_LIMIT = 16383
MCP_URL = os.environ.get(
    'MCP_URL', 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/'
)
PANDA_TOOL_PREFIX = 'panda_'

SYSTEM_PROMPT = """\
You are a PanDA production monitoring assistant for the ePIC experiment at the \
Electron Ion Collider. You answer questions about PanDA job and task status \
using MCP tools that query the production database.

Guidelines:
- Be concise. Use markdown tables for structured data.
- When showing job/task counts, summarize by status.
- For errors, show the top patterns with counts.
- When a user asks "what's happening" or "what's PanDA doing", start with panda_get_activity.
- For error investigation, use panda_error_summary first, then panda_diagnose_jobs for details.
- For a specific job, use panda_study_job.
- Default to 7 days unless the user specifies a time range.
- Keep responses focused — don't dump raw JSON, extract and present the key information.
- Use smaller limits (50 jobs, 20 tasks) unless the user asks for more.

When a query returns no results, do NOT just report "no results found." Instead:
- Consider whether the user's term might match a different field. For example, \
"epicproduction" is a processingtype, not a username. A term could be a username, \
taskname pattern, site name, working group, or processing type.
- Try a broader query (e.g. panda_get_activity or panda_list_tasks with fewer filters) \
to see what data exists, then narrow down.
- If you still find nothing after retrying, explain what you searched and suggest \
what the user might mean.
"""


class MCPClient:
    """Minimal MCP client using HTTP POST only — no SSE, no GET streams."""

    def __init__(self, url: str):
        self.url = url
        self.session_id = None
        self._request_id = 0
        self._http = httpx.AsyncClient(timeout=60)

    async def _post(self, method: str, params: dict | None = None):
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params:
            body["params"] = params
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = await self._http.post(self.url, json=body, headers=headers)
        resp.raise_for_status()
        if "Mcp-Session-Id" in resp.headers:
            self.session_id = resp.headers["Mcp-Session-Id"]
        return resp.json()

    async def initialize(self):
        return await self._post("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "panda-bot", "version": "1.0"},
        })

    async def list_tools(self):
        result = await self._post("tools/list")
        return result.get("result", {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict):
        result = await self._post("tools/call", {
            "name": name, "arguments": arguments,
        })
        return result.get("result", {})

    async def close(self):
        await self._http.aclose()


def mcp_tool_to_anthropic(tool):
    """Convert an MCP tool definition to Anthropic Messages API format."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool["inputSchema"],
    }


async def ask_claude(claude_client, mcp_url, message_text, conversation=None):
    """Send a message to Claude, using MCP for tool execution. Returns final text."""
    if conversation is not None:
        messages = conversation
    else:
        messages = [{"role": "user", "content": message_text}]

    mcp = MCPClient(mcp_url)
    try:
        await mcp.initialize()

        tools = await mcp.list_tools()
        anthropic_tools = [
            mcp_tool_to_anthropic(t) for t in tools
            if t["name"].startswith(PANDA_TOOL_PREFIX)
        ]
        logger.info(f"Discovered {len(anthropic_tools)} PanDA tools via MCP")

        for round_num in range(MAX_TOOL_ROUNDS):
            response = await claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=anthropic_tools,
                messages=messages,
            )

            logger.info(f"Claude response: stop_reason={response.stop_reason}")

            if response.stop_reason != "tool_use":
                text_parts = [
                    b.text for b in response.content if b.type == "text"
                ]
                reply = "\n".join(text_parts)
                logger.info(f"Final reply: {len(reply)} chars")
                return reply

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"Tool call: {block.name}({block.input})")
                    try:
                        result = mcp.call_tool(block.name, block.input)
                        result = await result
                        content = result.get("content", [])
                        result_text = ""
                        for item in content:
                            if isinstance(item, dict) and "text" in item:
                                result_text += item["text"]
                        if len(result_text) > MAX_RESULT_LEN:
                            result_text = (
                                result_text[:MAX_RESULT_LEN]
                                + '\n... (truncated)'
                            )
                    except Exception as e:
                        logger.exception(f"MCP tool {block.name} failed")
                        result_text = json.dumps({"error": str(e)})

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

            messages.append({"role": "user", "content": tool_results})
    finally:
        await mcp.close()

    return (
        "I hit the maximum number of tool calls. "
        "Please try a more specific question."
    )


class PandaBot:
    """Mattermost bot that answers PanDA production questions via Claude."""

    def __init__(self):
        self.mm_url = os.environ.get('MATTERMOST_URL', 'chat.epic-eic.org')
        self.mm_token = os.environ['MATTERMOST_TOKEN']
        self.mm_team = os.environ.get('MATTERMOST_TEAM', 'main')
        self.mm_channel_name = os.environ.get('MATTERMOST_CHANNEL', 'pandabot')
        self.mcp_url = MCP_URL

        self.claude = anthropic.AsyncAnthropic()

        self.driver = Driver({
            'url': self.mm_url,
            'token': self.mm_token,
            'scheme': 'https',
            'port': 443,
        })

        self.bot_user_id = None
        self.channel_id = None

    def start(self):
        """Connect to Mattermost and start listening."""
        logger.info(f"Connecting to {self.mm_url}...")
        self.driver.login()
        self.bot_user_id = self.driver.client.userid
        logger.info(f"Logged in as user {self.bot_user_id}")

        team = self.driver.teams.get_team_by_name(self.mm_team)
        channel = self.driver.channels.get_channel_by_name(
            team['id'], self.mm_channel_name
        )
        self.channel_id = channel['id']

        try:
            self.driver.channels.add_user(self.channel_id, options={
                'user_id': self.bot_user_id,
            })
            logger.info(f"Joined #{self.mm_channel_name}")
        except Exception:
            logger.info(f"Already a member of #{self.mm_channel_name}")

        logger.info(
            f"Listening on #{self.mm_channel_name} "
            f"(channel {self.channel_id}) in team {self.mm_team} "
            f"(MCP: {self.mcp_url})"
        )

        self.driver.init_websocket(self._handle_event)

    def _build_thread_conversation(self, root_id):
        """Fetch a Mattermost thread and build a Claude conversation from it."""
        thread = self.driver.posts.get_thread(root_id)
        posts = thread.get('posts', {})
        order = thread.get('order', [])

        messages = []
        for pid in order:
            p = posts.get(pid)
            if not p or not p.get('message', '').strip():
                continue
            role = "assistant" if p['user_id'] == self.bot_user_id else "user"
            if messages and messages[-1]['role'] == role:
                messages[-1]['content'] += "\n\n" + p['message'].strip()
            else:
                messages.append({
                    "role": role,
                    "content": p['message'].strip(),
                })

        if messages and messages[0]['role'] != 'user':
            messages = messages[1:]

        return messages if messages else None

    async def _handle_event(self, raw):
        """WebSocket event handler."""
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        event_type = event.get('event', '')
        if event_type and event_type != 'typing':
            logger.debug(f"WS event: {event_type}")

        if event_type != 'posted':
            return

        data = event.get('data', {})
        post_str = data.get('post')
        if not post_str:
            return

        try:
            post = json.loads(post_str)
        except (json.JSONDecodeError, TypeError):
            return

        post_channel = post.get('channel_id')
        post_user = post.get('user_id')
        post_type = post.get('type', '')
        logger.debug(
            f"Posted: channel={post_channel} user={post_user} "
            f"type={post_type} root_id={post.get('root_id', '')}"
        )

        if post_user == self.bot_user_id:
            logger.debug("Skipping own message")
            return

        if post_channel != self.channel_id:
            logger.debug(f"Skipping: channel {post_channel} != {self.channel_id}")
            return

        if post_type:
            logger.debug(f"Skipping system message type={post_type}")
            return

        message_text = post.get('message', '').strip()
        if not message_text:
            return

        post_id = post.get('id')
        root_id = post.get('root_id')
        logger.info(f"Message from {post_user}: {message_text[:100]}")

        try:
            conversation = None
            if root_id:
                conversation = self._build_thread_conversation(root_id)
            reply = await ask_claude(
                self.claude, self.mcp_url, message_text, conversation
            )
            logger.info(f"Got reply: {len(reply)} chars")
        except Exception:
            logger.exception("ask_claude failed")
            reply = "Sorry, I encountered an error processing your question."

        if len(reply) > MM_POST_LIMIT:
            reply = reply[:MM_POST_LIMIT - 20] + '\n\n... (truncated)'

        try:
            logger.info("Posting reply to Mattermost...")
            self.driver.posts.create_post(options={
                'channel_id': self.channel_id,
                'message': reply,
                'root_id': root_id or post_id,
            })
            logger.info("Reply posted successfully")
        except Exception:
            logger.exception("Failed to post reply")

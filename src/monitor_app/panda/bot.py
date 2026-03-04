"""
PanDA Mattermost bot — answers production monitoring questions using Claude with tool use.

Connects to Mattermost via WebSocket, listens for messages in a target channel,
and responds using Claude with PanDA monitoring tools discovered via MCP.

MCP transport: HTTP POST (JSON-RPC) to the Django MCP endpoint — the same
transport Claude Code uses. No SSE, no GET streams, no subprocesses.
"""

import asyncio
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
MEMORY_TURNS = 20
MCP_URL = os.environ.get(
    'MCP_URL', 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/'
)
BOT_TOOL_PREFIXES = ('panda_', 'emi_')
MEMORY_USERNAME = 'pandabot-community'

SYSTEM_PREAMBLE = """\
You are the PanDA bot for the ePIC experiment at the Electron Ion Collider. \
You answer questions about PanDA production and ePIC metadata using MCP tools.

CRITICAL: ALWAYS call a tool to answer questions. NEVER answer from memory or from \
examples in these instructions. The examples below show which tool to call, not what \
the answer is. The data changes constantly — you MUST query it live.

Guidelines:
- Be concise. Use markdown tables for structured data.
- When showing job/task counts, summarize by status.
- For errors, show the top patterns with counts.
- Default to 7 days unless the user specifies a time range.
- Keep responses focused — don't dump raw JSON, extract and present the key information.

When a query returns no results, do NOT just report "no results found." Instead:
- Consider whether the user's term might match a different field.
- Try a broader query to see what data exists, then narrow down.
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
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = await self._http.post(self.url, json=body, headers=headers)
        resp.raise_for_status()
        if "Mcp-Session-Id" in resp.headers:
            self.session_id = resp.headers["Mcp-Session-Id"]
        return resp.json()

    async def initialize(self):
        resp = await self._post("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "panda-bot", "version": "1.0"},
        })
        self.server_instructions = (
            resp.get("result", {}).get("instructions", "")
        )
        return resp

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


async def ask_claude(claude_client, mcp_url, message_text, conversation=None,
                     memory_context=""):
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
            if t["name"].startswith(BOT_TOOL_PREFIXES)
        ]
        logger.info(f"Discovered {len(anthropic_tools)} tools via MCP")

        system_prompt = SYSTEM_PREAMBLE
        if mcp.server_instructions:
            system_prompt += "\n" + mcp.server_instructions
        if memory_context:
            system_prompt += memory_context

        for round_num in range(MAX_TOOL_ROUNDS):
            response = await claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                system=system_prompt,
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
                        result = await mcp.call_tool(block.name, block.input)
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
        self.memory_context = ""

    async def _load_memory(self):
        """Load recent community Q&A history into system prompt context."""
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            result = await mcp.call_tool('swf_get_ai_memory', {
                'username': MEMORY_USERNAME,
                'turns': MEMORY_TURNS,
            })
            content = result.get('content', [])
            text = ''
            for item in content:
                if isinstance(item, dict) and 'text' in item:
                    text += item['text']
            if not text:
                return
            data = json.loads(text)
            items = data.get('items', [])
            if not items:
                return
            lines = []
            for item in items:
                role = 'Q' if item['role'] == 'user' else 'A'
                lines.append(f"{role}: {item['content']}")
            self.memory_context = (
                "\n\nRecent community Q&A history (for context, NOT as data source — "
                "always use tools for current data):\n"
                + "\n".join(lines)
            )
            logger.info(f"Loaded {len(items)} memory items into context")
        except Exception:
            logger.exception("Failed to load memory — continuing without it")
        finally:
            await mcp.close()

    async def _record_exchange(self, question, answer):
        """Record a Q&A exchange to community memory."""
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            for role, content in [('user', question), ('assistant', answer)]:
                await mcp.call_tool('swf_record_ai_memory', {
                    'username': MEMORY_USERNAME,
                    'session_id': 'mattermost',
                    'role': role,
                    'content': content,
                })
        except Exception:
            logger.exception("Failed to record exchange to memory")
        finally:
            await mcp.close()

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

        asyncio.get_event_loop().run_until_complete(self._load_memory())
        self.driver.init_websocket(self._handle_event)

    async def _build_thread_conversation(self, root_id):
        """Fetch a Mattermost thread and build a Claude conversation from it."""
        thread = await asyncio.to_thread(self.driver.posts.get_thread, root_id)
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

        asyncio.create_task(self._respond(message_text, post_id, root_id))

    async def _respond(self, message_text, post_id, root_id):
        """Process a message and post the reply. Runs as a background task."""
        try:
            conversation = None
            if root_id:
                conversation = await self._build_thread_conversation(root_id)
            reply = await ask_claude(
                self.claude, self.mcp_url, message_text, conversation,
                memory_context=self.memory_context,
            )
            logger.info(f"Got reply: {len(reply)} chars")
        except Exception:
            logger.exception("ask_claude failed")
            reply = "Sorry, I encountered an error processing your question."

        asyncio.create_task(self._record_exchange(message_text, reply))

        if len(reply) > MM_POST_LIMIT:
            reply = reply[:MM_POST_LIMIT - 20] + '\n\n... (truncated)'

        try:
            logger.info("Posting reply to Mattermost...")
            await asyncio.to_thread(
                self.driver.posts.create_post,
                options={
                    'channel_id': self.channel_id,
                    'message': reply,
                    'root_id': root_id or post_id,
                },
            )
            logger.info("Reply posted successfully")
        except Exception:
            logger.exception("Failed to post reply")

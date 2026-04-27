"""
SWF Testbed Mattermost bot — assists testbed developers via DMs and a shared channel.

Connects to Mattermost via WebSocket, listens for:
  - Direct messages from authorized users
  - Messages in the testbed channel

Access-controlled: only users in the TESTBED_USER_MAP can interact.
On each message, loads recent dialog from the database — all users, all
contexts — giving the bot full awareness. One soft privacy rule: don't
reveal DM content to others in the channel.

MCP transport: HTTP POST (JSON-RPC) — same as panda_bot. No SSE, no GET streams.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import anthropic
import httpx
from mattermostdriver import Driver

logger = logging.getLogger('testbed_bot')

MAX_TOOL_ROUNDS = 10
MAX_RESULT_LEN = 10000
MM_POST_LIMIT = 16383
MEMORY_TURNS = 30
MEMORY_USERNAME = 'testbedbot'
MCP_URL = os.environ.get(
    'MCP_URL', 'http://127.0.0.1:8001/swf-monitor/mcp/'
)
BOT_TOOL_PREFIXES = ('swf_', 'panda_', 'pcs_')

# Mattermost username -> testbed username
# Loaded from TESTBED_USER_MAP env var (JSON) or defaults below
DEFAULT_USER_MAP = {
    'wenaus': 'wenauseic',
    'rahmans': 'srahman1',
}

SYSTEM_PREAMBLE = """\
You are the SWF Testbed bot for the ePIC experiment at the Electron Ion Collider. \
You assist testbed developers with workflow operations, monitoring, and PanDA production \
using MCP tools.

You communicate via both direct messages and a shared Mattermost channel. \
Each message you receive is tagged with the sender's username and context \
(e.g. [wenaus in #swf-testbed-bot] or [wenaus in DM]). Your conversation \
history includes recent dialog across all users and contexts — refer back to \
earlier questions and answers naturally.

The current user's testbed username is: {testbed_username}

Privacy: a user's DM exchanges are their own business. Don't volunteer DM content \
to others in the channel.

CRITICAL: ALWAYS call a tool to answer questions. NEVER answer from memory or from \
examples in these instructions. Data changes constantly — query it live.

Active commands (start/stop testbed, start/stop workflow, kill agent, etc.) require the user \
to have a pandaserver02 account. Only execute active commands for the current user using \
their testbed username '{testbed_username}'. NEVER execute active commands on behalf of \
another user or with a different username. Read-only queries (status, list, logs, etc.) \
are fine for any mapped user.

Guidelines:
- Be concise. Use markdown tables for structured data.
- When a tool needs a username parameter, use '{testbed_username}'.
- For testbed operations (start, stop, status), always use the user's testbed username.
- Keep responses focused — extract and present the key information.

When a query returns no results, try broader queries before reporting nothing found.
"""


def load_user_map():
    """Load user mapping from env var or defaults."""
    raw = os.environ.get('TESTBED_USER_MAP', '')
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Invalid TESTBED_USER_MAP JSON, using defaults")
    return DEFAULT_USER_MAP.copy()


class MCPClient:
    """Minimal MCP client using HTTP POST only — no SSE, no GET streams."""

    def __init__(self, url: str, client_name: str = "testbed-bot"):
        self.url = url
        self.session_id = None
        self._request_id = 0
        self._http = httpx.AsyncClient(timeout=60)
        self._client_name = client_name

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
            "clientInfo": {"name": self._client_name, "version": "1.0"},
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


class TestbedBot:
    """Mattermost bot for testbed developers.

    On each message, loads recent dialog from the database — all users,
    all contexts — so the bot has full awareness. Access-controlled:
    only users in the user map can interact.
    """

    def __init__(self):
        self.mm_url = os.environ.get('MATTERMOST_URL', 'chat.epic-eic.org')
        self.mm_token = os.environ['TESTBED_BOT_TOKEN']
        self.mm_team = os.environ.get('MATTERMOST_TEAM', 'main')
        self.mm_channel_name = os.environ.get(
            'TESTBED_BOT_CHANNEL', 'swf-testbed-bot'
        )
        self.mcp_url = MCP_URL

        self.user_map = load_user_map()
        logger.info(f"User map: {self.user_map}")

        self.claude = anthropic.AsyncAnthropic()

        self.driver = Driver({
            'url': self.mm_url,
            'token': self.mm_token,
            'scheme': 'https',
            'port': 443,
        })

        self.bot_user_id = None
        self.channel_id = None
        self.system_prompt_base = SYSTEM_PREAMBLE
        self.anthropic_tools = []
        self.server_instructions = ""
        self._respond_lock = asyncio.Lock()
        self._mm_user_cache: dict[str, str] = {}

    async def _resolve_mm_username(self, mm_user_id):
        """Look up Mattermost username from user ID, with caching."""
        if mm_user_id in self._mm_user_cache:
            return self._mm_user_cache[mm_user_id]
        try:
            user = await asyncio.to_thread(
                self.driver.users.get_user, mm_user_id
            )
            username = user.get('username', '')
            self._mm_user_cache[mm_user_id] = username
            return username
        except Exception:
            logger.exception(f"Failed to resolve user {mm_user_id}")
            return ''

    def _build_system_prompt(self, testbed_username):
        """System prompt personalized for the current user."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        preamble = self.system_prompt_base.format(
            testbed_username=testbed_username,
        )
        prompt = f"Current date and time: {now}\n\n{preamble}"
        if self.server_instructions:
            prompt += "\n" + self.server_instructions
        return prompt

    async def _setup_mcp(self):
        """Discover tools and server instructions via MCP."""
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            tools = await mcp.list_tools()
            self.anthropic_tools = [
                mcp_tool_to_anthropic(t) for t in tools
                if t["name"].startswith(BOT_TOOL_PREFIXES)
            ]
            logger.info(f"Discovered {len(self.anthropic_tools)} tools via MCP")
            self.server_instructions = mcp.server_instructions or ""
            if self.server_instructions:
                self.system_prompt_base = (
                    SYSTEM_PREAMBLE + "\n" + self.server_instructions
                )
        except Exception:
            logger.exception("Failed MCP setup — will retry on first message")
        finally:
            await mcp.close()

    async def _load_recent_dialog(self):
        """Load recent dialog from the database — all users, all contexts."""
        mcp = MCPClient(self.mcp_url)
        messages = []
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
            if text:
                data = json.loads(text)
                for item in data.get('items', []):
                    messages.append({
                        "role": item['role'],
                        "content": item['content'],
                    })
                logger.info(f"Loaded {len(messages)} memory items")
        except Exception:
            logger.exception("Failed to load recent dialog")
        finally:
            await mcp.close()
        return messages

    async def _record_exchange(self, question, answer, post_id='', root_id=''):
        """Record a Q&A exchange to the unified memory."""
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            for role, content in [('user', question), ('assistant', answer)]:
                await mcp.call_tool('swf_record_ai_memory', {
                    'username': MEMORY_USERNAME,
                    'session_id': 'mattermost',
                    'role': role,
                    'content': content,
                    'namespace': post_id,
                    'project_path': root_id,
                })
        except Exception:
            logger.exception("Failed to record exchange")
        finally:
            await mcp.close()

    async def _build_thread_context(self, root_id):
        """Fetch full Mattermost thread and format as context."""
        try:
            thread = await asyncio.to_thread(
                self.driver.posts.get_thread, root_id
            )
            posts = thread.get('posts', {})
            order = thread.get('order', [])

            lines = []
            for pid in order:
                p = posts.get(pid)
                if not p or not p.get('message', '').strip():
                    continue
                speaker = "Bot" if p['user_id'] == self.bot_user_id else "User"
                lines.append(f"{speaker}: {p['message'].strip()}")

            return "\n".join(lines) if lines else None
        except Exception:
            logger.exception("Failed to fetch thread")
            return None

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
            f"Listening on #{self.mm_channel_name} + DMs "
            f"(MCP: {self.mcp_url}, users: {list(self.user_map.keys())})"
        )

        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._setup_mcp())
        self.driver.init_websocket(self._handle_event)

    async def _handle_event(self, raw):
        """WebSocket event handler — accepts channel messages and DMs."""
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
        channel_type = data.get('channel_type', '')

        if post_user == self.bot_user_id:
            return

        if post_type:
            return

        # Accept: our channel OR a DM
        is_our_channel = (post_channel == self.channel_id)
        is_dm = (channel_type == 'D')

        if not is_our_channel and not is_dm:
            return

        message_text = post.get('message', '').strip()
        if not message_text:
            return

        # Resolve Mattermost username and check authorization
        mm_username = await self._resolve_mm_username(post_user)
        if mm_username not in self.user_map:
            logger.info(f"Unauthorized user: {mm_username} ({post_user})")
            if is_dm:
                await asyncio.to_thread(
                    self.driver.posts.create_post,
                    options={
                        'channel_id': post_channel,
                        'message': (
                            "Sorry, I'm only available to authorized "
                            "testbed developers."
                        ),
                    },
                )
            return

        testbed_username = self.user_map[mm_username]
        post_id = post.get('id')
        root_id = post.get('root_id')

        context_tag = 'DM' if is_dm else f'#{self.mm_channel_name}'
        tagged_message = f"[{mm_username} in {context_tag}] {message_text}"
        logger.info(
            f"Message from {mm_username} "
            f"({'DM' if is_dm else 'channel'}): {message_text[:100]}"
        )

        asyncio.create_task(
            self._respond(tagged_message, testbed_username, post_channel, post_id, root_id)
        )

    async def _respond(self, tagged_message, testbed_username, reply_channel, post_id, root_id):
        """Process any message — channel or DM.

        Loads recent dialog from DB, runs Claude, records the exchange.
        Serialized via lock so recordings don't interleave.
        """
        async with self._respond_lock:
            try:
                messages = await self._load_recent_dialog()
                reply = await self._process_message(
                    messages, tagged_message, testbed_username, root_id
                )
                # Record inside lock so the next load sees this exchange
                await self._record_exchange(tagged_message, reply, post_id, root_id)
            except Exception:
                logger.exception("Testbed bot response task failed")
                reply = (
                    "Sorry, I hit an internal error while processing this "
                    "message. The exception was logged."
                )

        if len(reply) > MM_POST_LIMIT:
            reply = reply[:MM_POST_LIMIT - 20] + '\n\n... (truncated)'

        try:
            logger.info("Posting reply to Mattermost...")
            await asyncio.to_thread(
                self.driver.posts.create_post,
                options={
                    'channel_id': reply_channel,
                    'message': reply,
                    'root_id': root_id or post_id,
                },
            )
            logger.info("Reply posted successfully")
        except Exception:
            logger.exception("Failed to post reply")

    async def _process_message(self, messages, message_text, testbed_username, root_id):
        """Run the Claude conversation loop for one user message.

        Returns the final reply text. The messages list is ephemeral —
        loaded from DB for this request and discarded after.
        """
        # Build user message with full thread context if it's a reply
        user_content = message_text
        if root_id:
            thread_context = await self._build_thread_context(root_id)
            if thread_context:
                user_content = (
                    f"[Thread conversation so far:\n{thread_context}\n]\n"
                    f"New reply: {message_text}"
                )

        messages.append({"role": "user", "content": user_content})

        reply = "Sorry, I encountered an error processing your question."

        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()

            if not self.anthropic_tools:
                tools = await mcp.list_tools()
                self.anthropic_tools = [
                    mcp_tool_to_anthropic(t) for t in tools
                    if t["name"].startswith(BOT_TOOL_PREFIXES)
                ]

            system = self._build_system_prompt(testbed_username)

            for _round in range(MAX_TOOL_ROUNDS):
                response = await self.claude.beta.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    cache_control={"type": "ephemeral"},
                    system=system,
                    tools=self.anthropic_tools,
                    messages=messages,
                    betas=["context-management-2025-06-27"],
                    context_management={
                        "edits": [{
                            "type": "clear_tool_uses_20250919",
                            "trigger": {
                                "type": "input_tokens",
                                "value": 80000,
                            },
                            "keep": {"type": "tool_uses", "value": 3},
                        }]
                    },
                )
                logger.info(
                    f"Claude response: stop_reason={response.stop_reason}"
                )

                if response.stop_reason != "tool_use":
                    text_parts = [
                        b.text for b in response.content if b.type == "text"
                    ]
                    reply = "\n".join(text_parts)
                    break

                messages.append(
                    {"role": "assistant", "content": response.content}
                )
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
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
                messages.append(
                    {"role": "user", "content": tool_results}
                )
            else:
                reply = (
                    "I hit the maximum number of tool calls. "
                    "Please try a more specific question."
                )

            logger.info(f"Got reply: {len(reply)} chars")

        except Exception:
            logger.exception("ask_claude failed")
        finally:
            await mcp.close()

        return reply

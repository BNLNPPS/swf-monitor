"""
SWF Testbed Mattermost bot — assists testbed developers via DMs and a shared channel.

Connects to Mattermost via WebSocket, listens for:
  - Direct messages from authorized users
  - Messages in the testbed channel

Access-controlled: only users in the TESTBED_USER_MAP can interact.
Each user gets their own conversation history keyed by Mattermost user ID.

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
MEMORY_TURNS = 20
MAX_SESSION_MESSAGES = 200
MCP_URL = os.environ.get(
    'MCP_URL', 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/'
)
BOT_TOOL_PREFIXES = ('swf_', 'panda_', 'emi_')
BOT_EXTRA_TOOLS = ()
MEMORY_USERNAME_PREFIX = 'testbedbot'

# Mattermost username -> testbed username
# Loaded from TESTBED_USER_MAP env var (JSON) or defaults below
DEFAULT_USER_MAP = {
    'wenaus': 'wenauseic',
}

SYSTEM_PREAMBLE = """\
You are the SWF Testbed bot for the ePIC experiment at the Electron Ion Collider. \
You assist testbed developers with workflow operations, monitoring, and PanDA production \
using MCP tools.

You communicate via both direct messages and a shared Mattermost channel. \
Each user has their own conversation context.

The current user's testbed username is: {testbed_username}

CRITICAL: ALWAYS call a tool to answer questions. NEVER answer from memory or from \
examples in these instructions. Data changes constantly — query it live.

You have access to swf_get_ai_memory which retrieves conversation history from \
previous sessions. Use it when someone references something from a past conversation. \
Call it with username='testbedbot-{testbed_username}' and a turns count.

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


class UserSession:
    """Per-user conversation state."""

    def __init__(self, mm_username, testbed_username):
        self.mm_username = mm_username
        self.testbed_username = testbed_username
        self.messages = []
        self.lock = asyncio.Lock()

    def trim_messages(self):
        if len(self.messages) <= MAX_SESSION_MESSAGES:
            return
        self.messages = self.messages[-MAX_SESSION_MESSAGES:]
        if self.messages and self.messages[0]['role'] != 'user':
            self.messages = self.messages[1:]
        logger.info(
            f"Trimmed {self.mm_username} conversation to "
            f"{len(self.messages)} messages"
        )


class TestbedBot:
    """Mattermost bot for testbed developers.

    Listens on both a channel and DMs. Only authorized users (those in the
    user map) can interact. Each user gets per-user conversation history.
    """

    def __init__(self):
        self.mm_url = os.environ.get('MATTERMOST_URL', 'chat.epic-eic.org')
        self.mm_token = os.environ['TESTBED_BOT_TOKEN']
        self.mm_team = os.environ.get('MATTERMOST_TEAM', 'main')
        self.mm_channel_name = os.environ.get(
            'TESTBED_BOT_CHANNEL', 'testbedbot'
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
        self.sessions: dict[str, UserSession] = {}
        self.anthropic_tools = []
        self.server_instructions = ""
        # Cache: mm_user_id -> mm_username
        self._mm_user_cache: dict[str, str] = {}

    def _get_session(self, mm_user_id, mm_username):
        """Get or create a per-user session."""
        if mm_user_id not in self.sessions:
            testbed_username = self.user_map.get(mm_username)
            if not testbed_username:
                return None
            self.sessions[mm_user_id] = UserSession(
                mm_username, testbed_username
            )
            logger.info(
                f"New session for {mm_username} -> {testbed_username}"
            )
        return self.sessions[mm_user_id]

    def _build_system_prompt(self, session: UserSession):
        """System prompt personalized for this user."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        preamble = SYSTEM_PREAMBLE.format(
            testbed_username=session.testbed_username,
        )
        prompt = f"Current date and time: {now}\n\n{preamble}"
        if self.server_instructions:
            prompt += "\n" + self.server_instructions
        return prompt

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

    async def _setup_mcp(self):
        """Discover tools and server instructions via MCP."""
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            tools = await mcp.list_tools()
            self.anthropic_tools = [
                mcp_tool_to_anthropic(t) for t in tools
                if t["name"].startswith(BOT_TOOL_PREFIXES)
                or t["name"] in BOT_EXTRA_TOOLS
            ]
            logger.info(f"Discovered {len(self.anthropic_tools)} tools via MCP")
            self.server_instructions = mcp.server_instructions or ""
        except Exception:
            logger.exception("Failed MCP setup — will retry on first message")
        finally:
            await mcp.close()

    async def _load_memory(self, session: UserSession):
        """Load per-user memory as conversation history."""
        memory_user = f"{MEMORY_USERNAME_PREFIX}-{session.testbed_username}"
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            result = await mcp.call_tool('swf_get_ai_memory', {
                'username': memory_user,
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
            for item in items:
                session.messages.append({
                    "role": item['role'],
                    "content": item['content'],
                })
            logger.info(
                f"Loaded {len(items)} memory items for {session.mm_username}"
            )
        except Exception:
            logger.exception(
                f"Failed to load memory for {session.mm_username}"
            )
        finally:
            await mcp.close()

    async def _record_exchange(self, session: UserSession, question, answer):
        """Record a Q&A exchange to per-user memory."""
        memory_user = f"{MEMORY_USERNAME_PREFIX}-{session.testbed_username}"
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            for role, content in [('user', question), ('assistant', answer)]:
                await mcp.call_tool('swf_record_ai_memory', {
                    'username': memory_user,
                    'session_id': 'mattermost',
                    'role': role,
                    'content': content,
                })
        except Exception:
            logger.exception("Failed to record exchange to memory")
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

    def _is_dm_channel(self, channel_type):
        """Check if channel type is a direct message."""
        return channel_type in ('D',)

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
        is_dm = self._is_dm_channel(channel_type)

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

        session = self._get_session(post_user, mm_username)

        # Load memory on first interaction
        if not session.messages:
            await self._load_memory(session)

        post_id = post.get('id')
        root_id = post.get('root_id')
        logger.info(
            f"Message from {mm_username} "
            f"({'DM' if is_dm else 'channel'}): {message_text[:100]}"
        )

        asyncio.create_task(
            self._respond(session, message_text, post_channel, post_id, root_id)
        )

    async def _respond(self, session, message_text, channel_id, post_id, root_id):
        """Process a message and post the reply."""
        async with session.lock:
            reply = await self._process_message(session, message_text, root_id)

        asyncio.create_task(
            self._record_exchange(session, message_text, reply)
        )

        if len(reply) > MM_POST_LIMIT:
            reply = reply[:MM_POST_LIMIT - 20] + '\n\n... (truncated)'

        try:
            logger.info(f"Posting reply to {session.mm_username}...")
            await asyncio.to_thread(
                self.driver.posts.create_post,
                options={
                    'channel_id': channel_id,
                    'message': reply,
                    'root_id': root_id or post_id,
                },
            )
            logger.info("Reply posted successfully")
        except Exception:
            logger.exception("Failed to post reply")

    async def _process_message(self, session, message_text, root_id):
        """Run the Claude conversation loop for one user message."""
        user_content = message_text
        if root_id:
            thread_context = await self._build_thread_context(root_id)
            if thread_context:
                user_content = (
                    f"[Thread conversation so far:\n{thread_context}\n]\n"
                    f"New reply: {message_text}"
                )

        msg_start = len(session.messages)
        session.messages.append({"role": "user", "content": user_content})
        session.trim_messages()

        reply = "Sorry, I encountered an error processing your question."

        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()

            if not self.anthropic_tools:
                tools = await mcp.list_tools()
                self.anthropic_tools = [
                    mcp_tool_to_anthropic(t) for t in tools
                    if t["name"].startswith(BOT_TOOL_PREFIXES)
                    or t["name"] in BOT_EXTRA_TOOLS
                ]

            system = self._build_system_prompt(session)

            for _round in range(MAX_TOOL_ROUNDS):
                response = await self.claude.beta.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    cache_control={"type": "ephemeral"},
                    system=system,
                    tools=self.anthropic_tools,
                    messages=session.messages,
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

                session.messages.append(
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
                session.messages.append(
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

        # Consolidate: replace intermediate messages with clean Q&A pair
        session.messages = session.messages[:msg_start]
        session.messages.append({"role": "user", "content": user_content})
        session.messages.append({"role": "assistant", "content": reply})

        return reply

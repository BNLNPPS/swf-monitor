"""
PanDA Mattermost bot — answers production monitoring questions using Claude with tool use.

Connects to Mattermost via WebSocket, listens for messages in a target channel,
and responds using Claude with PanDA monitoring tools discovered via MCP.

Maintains a persistent conversation session — Claude sees the full channel dialog
history, just like any chatbot. Cross-session memory is loaded at startup from
the swf_ai_memory system and new exchanges are recorded for future sessions.

MCP transport: HTTP POST (JSON-RPC) to the Django MCP endpoint — the same
transport Claude Code uses. No SSE, no GET streams, no subprocesses.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import anthropic
import httpx
from mattermostdriver import Driver

logger = logging.getLogger('panda_bot')

MAX_TOOL_ROUNDS = 10
MAX_RESULT_LEN = 10000
MM_POST_LIMIT = 16383
MEMORY_TURNS = 20
MAX_SESSION_MESSAGES = 200
MCP_URL = os.environ.get(
    'MCP_URL', 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/'
)
BOT_TOOL_PREFIXES = ('panda_', 'pcs_')
BOT_EXTRA_TOOLS = ('swf_get_ai_memory',)
MEMORY_USERNAME = 'pandabot-community'

SYSTEM_PREAMBLE = """\
You are the PanDA bot for the ePIC experiment at the Electron Ion Collider. \
You use MCP tools to answer questions about PanDA production and the configuration of production \
tasks based on physics inputs using the Physics Configuration System (PCS).

You communicate via Mattermost — in a shared channel, in direct messages (DMs), \
and when @mentioned in any channel. You do not know which context you are in \
unless told. Your channel conversation and each user's DM conversation are \
SEPARATE — you cannot see a user's DM history from the channel, or channel \
history from a DM. If someone references something from a different context, \
tell them honestly that your conversations are separate. \
You maintain full awareness of the ongoing conversation — refer back to earlier \
questions and answers naturally.

CRITICAL: ALWAYS call a tool to answer questions. NEVER answer from memory or from \
examples in these instructions. The examples below show which tool to call, not what \
the answer is. The data changes constantly — you MUST query it live.

You have access to swf_get_ai_memory which retrieves conversation history from \
previous sessions. Use it when someone references something from a past conversation \
or when deeper context would help answer a question. For channel conversations use \
username='pandabot-community'. For DMs or @mention conversations, use \
username='pandabot-{mm_username}' where mm_username is the Mattermost username of \
the person you are talking to.

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


MEMORY_USERNAME_PREFIX = 'pandabot'


class UserSession:
    """Per-user conversation state for DMs."""

    def __init__(self, mm_username):
        self.mm_username = mm_username
        self.messages = []
        self.lock = asyncio.Lock()


class PandaBot:
    """Mattermost bot that answers PanDA production questions via Claude.

    Channel messages use a shared conversation. DMs and @mentions use
    per-user sessions with per-user persistent memory.
    """

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
        # Shared channel conversation
        self.messages = []
        self.system_prompt = SYSTEM_PREAMBLE
        self.anthropic_tools = []
        self._respond_lock = asyncio.Lock()
        self._active_threads = set()
        # Per-user DM sessions
        self.sessions: dict[str, UserSession] = {}
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

    def _get_session(self, mm_user_id, mm_username):
        """Get or create a per-user DM session."""
        if mm_user_id not in self.sessions:
            self.sessions[mm_user_id] = UserSession(mm_username)
            logger.info(f"New DM session for {mm_username}")
        return self.sessions[mm_user_id]

    def _build_system_prompt(self):
        """System prompt with current datetime."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        return f"Current date and time: {now}\n\n{self.system_prompt}"

    async def _setup_mcp(self):
        """Discover tools and build system prompt via MCP."""
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
            if mcp.server_instructions:
                self.system_prompt = (
                    SYSTEM_PREAMBLE + "\n" + mcp.server_instructions
                )
        except Exception:
            logger.exception("Failed MCP setup — will retry on first message")
        finally:
            await mcp.close()

    async def _load_memory(self, memory_user, messages_list):
        """Load recent Q&A history into a messages list."""
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
                messages_list.append({
                    "role": item['role'],
                    "content": item['content'],
                })
            logger.info(
                f"Loaded {len(items)} memory items for {memory_user}"
            )
        except Exception:
            logger.exception(f"Failed to load memory for {memory_user}")
        finally:
            await mcp.close()

    async def _record_exchange(self, memory_user, question, answer):
        """Record a Q&A exchange to memory."""
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
            logger.exception(f"Failed to record exchange for {memory_user}")
        finally:
            await mcp.close()

    async def _build_thread_context(self, root_id):
        """Fetch full Mattermost thread and format as context.

        Thread replies are not visible in the main channel, so Claude
        has no record of them in the session conversation. This provides
        the full thread history for thread replies.
        """
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
            f"Listening on #{self.mm_channel_name} "
            f"(channel {self.channel_id}) in team {self.mm_team} "
            f"(MCP: {self.mcp_url})"
        )

        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._setup_mcp())
        loop.run_until_complete(self._load_memory(MEMORY_USERNAME, self.messages))
        self._load_active_threads()
        self.driver.init_websocket(self._handle_event)

    THREADS_STATE_KEY = 'pandabot_active_threads'

    def _load_active_threads(self):
        """Load active thread IDs from PersistentState."""
        from monitor_app.models import PersistentState
        try:
            obj = PersistentState.objects.get(id=1)
            threads = obj.state_data.get(self.THREADS_STATE_KEY, [])
            self._active_threads = set(threads)
            logger.info(f"Loaded {len(self._active_threads)} active threads")
        except PersistentState.DoesNotExist:
            self._active_threads = set()

    def _save_active_threads(self):
        """Persist active thread IDs. Keep only the most recent 200."""
        from monitor_app.models import PersistentState
        threads = list(self._active_threads)[-200:]
        self._active_threads = set(threads)
        obj, _ = PersistentState.objects.get_or_create(id=1, defaults={'state_data': {}})
        obj.state_data[self.THREADS_STATE_KEY] = threads
        obj.save()

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

        channel_type = data.get('channel_type', '')

        if post_user == self.bot_user_id:
            logger.debug("Skipping own message")
            return

        if post_type:
            logger.debug(f"Skipping system message type={post_type}")
            return

        # Accept: our channel, a DM, an @mention, or a thread we're in
        is_our_channel = (post_channel == self.channel_id)
        is_dm = (channel_type == 'D')
        mentions_str = data.get('mentions', '')
        is_mention = self.bot_user_id and self.bot_user_id in mentions_str
        root_id = post.get('root_id', '')
        is_active_thread = root_id in self._active_threads
        if not is_our_channel and not is_dm and not is_mention and not is_active_thread:
            return

        message_text = post.get('message', '').strip()
        if not message_text:
            return

        post_id = post.get('id')
        is_personal = is_dm or (is_mention and not is_our_channel) or (is_active_thread and not is_our_channel)
        source = 'DM' if is_dm else ('thread' if is_active_thread and not is_our_channel else ('mention' if is_mention and not is_our_channel else 'channel'))
        logger.info(f"Message from {post_user} ({source}): {message_text[:100]}")

        if is_personal:
            # Per-user session for DMs, @mentions, and their follow-up threads
            mm_username = await self._resolve_mm_username(post_user)
            session = self._get_session(post_user, mm_username)
            if not session.messages:
                memory_user = f"{MEMORY_USERNAME_PREFIX}-{mm_username}"
                await self._load_memory(memory_user, session.messages)
            asyncio.create_task(self._respond_personal(session, message_text, post_channel, post_id, root_id))
        else:
            # Shared channel conversation
            asyncio.create_task(self._respond_channel(message_text, post_channel, post_id, root_id))

    async def _respond_channel(self, message_text, reply_channel, post_id, root_id):
        """Process a channel message using the shared conversation."""
        async with self._respond_lock:
            reply = await self._process_message(self.messages, message_text, root_id)

        asyncio.create_task(self._record_exchange(MEMORY_USERNAME, message_text, reply))
        await self._post_reply(reply, reply_channel, post_id, root_id)

    async def _respond_personal(self, session, message_text, reply_channel, post_id, root_id):
        """Process a DM/@mention using per-user conversation."""
        async with session.lock:
            reply = await self._process_message(session.messages, message_text, root_id)

        memory_user = f"{MEMORY_USERNAME_PREFIX}-{session.mm_username}"
        asyncio.create_task(self._record_exchange(memory_user, message_text, reply))
        await self._post_reply(reply, reply_channel, post_id, root_id)

    async def _post_reply(self, reply, reply_channel, post_id, root_id):

        if len(reply) > MM_POST_LIMIT:
            reply = reply[:MM_POST_LIMIT - 20] + '\n\n... (truncated)'

        thread_root = root_id or post_id
        try:
            logger.info("Posting reply to Mattermost...")
            await asyncio.to_thread(
                self.driver.posts.create_post,
                options={
                    'channel_id': reply_channel,
                    'message': reply,
                    'root_id': thread_root,
                },
            )
            # Track this thread so we respond to follow-ups (persisted)
            if thread_root not in self._active_threads:
                self._active_threads.add(thread_root)
                await asyncio.to_thread(self._save_active_threads)
            logger.info("Reply posted successfully")
        except Exception:
            logger.exception("Failed to post reply")

    async def _process_message(self, messages, message_text, root_id):
        """Run the Claude conversation loop for one user message.

        Returns the final reply text. Consolidates tool-use messages
        afterward so the conversation history stays clean.
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
        # Trim if needed
        if len(messages) > MAX_SESSION_MESSAGES:
            del messages[:len(messages) - MAX_SESSION_MESSAGES]
            if messages and messages[0]['role'] != 'user':
                del messages[0]
        msg_start = len(messages) - 1  # index of the user message we just added

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

            system = self._build_system_prompt()

            for _round in range(MAX_TOOL_ROUNDS):
                response = await self.claude.beta.messages.create(
                    # DO NOT change model without user approval
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

                # Tool use — append intermediate messages for this round
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

        # Consolidate: replace all intermediate messages with clean Q&A pair
        del messages[msg_start:]
        messages.append({"role": "user", "content": user_content})
        messages.append({"role": "assistant", "content": reply})

        return reply

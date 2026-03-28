"""
PanDA Mattermost bot — answers production monitoring questions using Claude with tool use.

Connects to Mattermost via WebSocket, listens for messages in a target channel,
and responds using Claude with PanDA monitoring tools discovered via MCP.

On each question, loads recent dialog from the database — all users, all
contexts — giving the bot full awareness of recent conversations. One soft
privacy rule: don't reveal DM content to others in the channel.

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
MEMORY_TURNS = 30
MEMORY_USERNAME = 'pandabot'
MCP_URL = os.environ.get(
    'MCP_URL', 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/'
)
BOT_TOOL_PREFIXES = ('panda_', 'pcs_')

SYSTEM_PREAMBLE = """\
You are the PanDA bot for the ePIC experiment at the Electron Ion Collider. \
You use MCP tools to answer questions about PanDA production and the configuration of production \
tasks based on physics inputs using the Physics Configuration System (PCS).

You communicate via Mattermost — in a shared channel, in direct messages (DMs), \
and when @mentioned in any channel. Each message you receive is tagged with the \
sender's username and context (e.g. [wenaus in #pandabot] or [wenaus in DM]). \
Your conversation history includes recent dialog across all users and contexts — \
refer back to earlier questions and answers naturally.

Privacy: a user's DM exchanges are their own business. Don't volunteer DM content \
to others in the channel.

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


class PandaBot:
    """Mattermost bot that answers PanDA production questions via Claude.

    On each message, loads recent dialog from the database — all users,
    all contexts — so the bot has full awareness of the community.
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
        self.system_prompt = SYSTEM_PREAMBLE
        self.anthropic_tools = []
        self._respond_lock = asyncio.Lock()
        self._active_threads = set()
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
        mm_username = await self._resolve_mm_username(post_user)

        if is_dm:
            context_tag = 'DM'
        elif is_our_channel:
            context_tag = f'#{self.mm_channel_name}'
        else:
            channel_name = data.get('channel_name', 'unknown')
            context_tag = f'#{channel_name}'

        tagged_message = f"[{mm_username} in {context_tag}] {message_text}"
        source = 'DM' if is_dm else ('mention' if is_mention and not is_our_channel else 'channel')
        logger.info(f"Message from {mm_username} ({source}): {message_text[:100]}")

        asyncio.create_task(self._respond(tagged_message, post_channel, post_id, root_id))

    async def _respond(self, tagged_message, reply_channel, post_id, root_id):
        """Process any message — channel, DM, or mention.

        Loads recent dialog from DB, runs Claude, records the exchange.
        Serialized via lock so recordings don't interleave.
        """
        async with self._respond_lock:
            messages = await self._load_recent_dialog()
            reply = await self._process_message(messages, tagged_message, root_id)
            # Record inside lock so the next load sees this exchange
            await self._record_exchange(tagged_message, reply, post_id, root_id)

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

        return reply

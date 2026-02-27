"""
PanDA Mattermost bot — answers production monitoring questions using Claude with tool use.

Connects to Mattermost via WebSocket, listens for messages in a target channel,
and responds using Claude Sonnet with direct access to PanDA query functions.
"""

import json
import logging
import os
import ssl

import anthropic
from mattermostdriver import Driver

from monitor_app.panda import queries

logger = logging.getLogger('panda_bot')

MAX_TOOL_ROUNDS = 10
MAX_RESULT_LEN = 30000
MM_POST_LIMIT = 16383

SYSTEM_PROMPT = """\
You are a PanDA production monitoring assistant for the ePIC experiment at the \
Electron Ion Collider. You answer questions about PanDA job and task status by \
querying the production database.

Guidelines:
- Be concise. Use markdown tables for structured data.
- When showing job/task counts, summarize by status.
- For errors, show the top patterns with counts.
- When a user asks "what's happening" or "what's PanDA doing", start with get_activity.
- For error investigation, use error_summary first, then diagnose_jobs for details.
- For a specific job, use study_job.
- Default to 7 days unless the user specifies a time range.
- Keep responses focused — don't dump raw JSON, extract and present the key information.
- If a query returns no results, say so clearly.
- Use smaller limits (50 jobs, 20 tasks) unless the user asks for more.
"""

PANDA_TOOLS = [
    {
        "name": "get_activity",
        "description": (
            "Quick overview of PanDA activity — aggregate job and task counts "
            "by status, user, and site. No individual records. Use this first "
            "to answer 'what is PanDA doing?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Time window in days (default 1)",
                    "default": 1,
                },
                "username": {
                    "type": "string",
                    "description": "Filter by job owner. Supports SQL LIKE with %.",
                },
                "site": {
                    "type": "string",
                    "description": "Filter by computing site. Supports SQL LIKE with %.",
                },
                "workinggroup": {
                    "type": "string",
                    "description": "Filter tasks by working group (e.g. 'EIC').",
                },
            },
        },
    },
    {
        "name": "list_jobs",
        "description": (
            "List individual PanDA job records with summary statistics. "
            "Returns job details including pandaid, status, site, user, task ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Time window in days (default 7)",
                    "default": 7,
                },
                "status": {
                    "type": "string",
                    "description": "Filter by jobstatus (e.g. 'failed', 'finished', 'running').",
                },
                "username": {
                    "type": "string",
                    "description": "Filter by job owner. Supports SQL LIKE with %.",
                },
                "site": {
                    "type": "string",
                    "description": "Filter by computing site. Supports SQL LIKE with %.",
                },
                "taskid": {
                    "type": "integer",
                    "description": "Filter by JEDI task ID.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max jobs to return (default 50).",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "diagnose_jobs",
        "description": (
            "Get failed/cancelled PanDA jobs with full error details. "
            "Shows error components (pilot, executor, DDM, etc.) and diagnostics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Time window in days (default 7)",
                    "default": 7,
                },
                "username": {
                    "type": "string",
                    "description": "Filter by job owner. Supports SQL LIKE with %.",
                },
                "site": {
                    "type": "string",
                    "description": "Filter by computing site. Supports SQL LIKE with %.",
                },
                "taskid": {
                    "type": "integer",
                    "description": "Filter by JEDI task ID.",
                },
                "error_component": {
                    "type": "string",
                    "description": "Filter to errors in this component (pilot, executor, ddm, brokerage, dispatcher, supervisor, taskbuffer).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max jobs to return (default 50).",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "List JEDI task records with summary statistics. "
            "Tasks are higher-level units — each spawns one or more jobs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Time window in days (default 7)",
                    "default": 7,
                },
                "status": {
                    "type": "string",
                    "description": "Filter by task status (e.g. 'done', 'failed', 'running').",
                },
                "username": {
                    "type": "string",
                    "description": "Filter by task owner. Supports SQL LIKE with %.",
                },
                "taskname": {
                    "type": "string",
                    "description": "Filter by task name. Supports SQL LIKE with %.",
                },
                "workinggroup": {
                    "type": "string",
                    "description": "Filter by working group (e.g. 'EIC').",
                },
                "taskid": {
                    "type": "integer",
                    "description": "Filter by specific JEDI task ID.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max tasks to return (default 20).",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "error_summary",
        "description": (
            "Aggregate error summary across failed PanDA jobs, ranked by frequency. "
            "Shows top error patterns with counts, affected tasks, users, and sites."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Time window in days (default 10)",
                    "default": 10,
                },
                "username": {
                    "type": "string",
                    "description": "Filter by job owner. Supports SQL LIKE with %.",
                },
                "site": {
                    "type": "string",
                    "description": "Filter by computing site. Supports SQL LIKE with %.",
                },
                "taskid": {
                    "type": "integer",
                    "description": "Filter by JEDI task ID.",
                },
                "error_source": {
                    "type": "string",
                    "description": "Filter to one component (pilot, executor, ddm, brokerage, dispatcher, supervisor, taskbuffer).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max error patterns to return (default 20).",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "study_job",
        "description": (
            "Deep study of a single PanDA job — full record, files, errors, "
            "log URLs, harvester worker info, and parent task context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pandaid": {
                    "type": "integer",
                    "description": "The PanDA job ID to study.",
                },
            },
            "required": ["pandaid"],
        },
    },
]

TOOL_DISPATCH = {
    "get_activity": queries.get_activity,
    "list_jobs": queries.list_jobs,
    "diagnose_jobs": queries.diagnose_jobs,
    "list_tasks": queries.list_tasks,
    "error_summary": queries.error_summary,
    "study_job": queries.study_job,
}


def _json_default(obj):
    """JSON serializer for objects not serializable by default."""
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    return str(obj)


def execute_tool(name, tool_input):
    """Call a PanDA query function and return JSON string result."""
    func = TOOL_DISPATCH.get(name)
    if not func:
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        result = func(**tool_input)
        text = json.dumps(result, default=_json_default)
        if len(text) > MAX_RESULT_LEN:
            text = text[:MAX_RESULT_LEN] + '\n... (truncated)'
        return text
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return json.dumps({"error": str(e)})


def ask_claude(client, message_text, conversation=None):
    """Send a message to Claude and handle the tool-use loop. Returns final text."""
    if conversation is None:
        messages = [{"role": "user", "content": message_text}]
    else:
        messages = conversation

    for round_num in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=PANDA_TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            # Extract text from response
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts)

        # Process tool calls
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                logger.info(f"Tool call: {block.name}({block.input})")
                result_text = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

        messages.append({"role": "user", "content": tool_results})

    return "I hit the maximum number of tool calls. Here's what I found so far — please try a more specific question."


class PandaBot:
    """Mattermost bot that answers PanDA production questions via Claude."""

    def __init__(self):
        self.mm_url = os.environ.get('MATTERMOST_URL', 'chat.epic-eic.org')
        self.mm_token = os.environ['MATTERMOST_TOKEN']
        self.mm_team = os.environ.get('MATTERMOST_TEAM', 'main')
        self.mm_channel_name = os.environ.get('MATTERMOST_CHANNEL', 'pandabot')

        self.claude = anthropic.Anthropic()

        self.driver = Driver({
            'url': self.mm_url,
            'token': self.mm_token,
            'scheme': 'https',
            'port': 443,
            'websocket_kw_args': {
                'ssl': ssl.create_default_context(),
            },
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
        logger.info(
            f"Listening on #{self.mm_channel_name} "
            f"(channel {self.channel_id}) in team {self.mm_team}"
        )

        self.driver.init_websocket(self._handle_event)

    async def _handle_event(self, raw):
        """WebSocket event handler."""
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        if event.get('event') != 'posted':
            return

        data = event.get('data', {})
        post_str = data.get('post')
        if not post_str:
            return

        try:
            post = json.loads(post_str)
        except (json.JSONDecodeError, TypeError):
            return

        # Ignore own messages
        if post.get('user_id') == self.bot_user_id:
            return

        # Only target channel
        if post.get('channel_id') != self.channel_id:
            return

        # Ignore thread replies (only respond to root posts)
        if post.get('root_id'):
            return

        message_text = post.get('message', '').strip()
        if not message_text:
            return

        post_id = post.get('id')
        logger.info(f"Message from {post.get('user_id')}: {message_text[:100]}")

        try:
            reply = ask_claude(self.claude, message_text)
        except Exception:
            logger.exception("Claude API call failed")
            reply = "Sorry, I encountered an error processing your question."

        # Truncate for Mattermost post limit
        if len(reply) > MM_POST_LIMIT:
            reply = reply[:MM_POST_LIMIT - 20] + '\n\n... (truncated)'

        try:
            self.driver.posts.create_post(options={
                'channel_id': self.channel_id,
                'message': reply,
                'root_id': post_id,
            })
        except Exception:
            logger.exception("Failed to post reply")

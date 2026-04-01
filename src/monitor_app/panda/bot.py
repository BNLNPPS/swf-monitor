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
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# ChromaDB requires sqlite3 >= 3.35; RHEL8 ships 3.26.
# pysqlite3-binary bundles a modern sqlite3 — swap BEFORE any chromadb import.
try:
    __import__("pysqlite3")
    sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

import anthropic
import httpx
import numpy as np
from mattermostdriver import Driver
from sentence_transformers import SentenceTransformer

logger = logging.getLogger('panda_bot')

MAX_TOOL_ROUNDS = 10
MAX_RESULT_LEN = 10000
MM_POST_LIMIT = 16383
MEMORY_TURNS = 30
MEMORY_USERNAME = 'pandabot'
MCP_URL = os.environ.get(
    'MCP_URL', 'https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/'
)
BOT_TOOL_PREFIXES = ('panda_', 'pcs_', 'epic_')

# Stdio MCP servers — launched as subprocesses at startup.
# update_commands: if present, the server can be updated via bot_manage_servers.
STDIO_MCP_SERVERS = []

_xrootd_server = os.environ.get('XROOTD_MCP_SERVER')
if _xrootd_server:
    STDIO_MCP_SERVERS.append({
        'name': 'xrootd',
        'source': 'github.com/eic/xrootd-mcp-server',
        'command': [
            os.environ.get('NODE_PATH', '/eic/u/wenauseic/.nvm/versions/node/v22.17.0/bin/node'),
            '/data/wenauseic/github/xrootd-mcp-server/build/src/index.js',
        ],
        'env': {
            'XROOTD_SERVER': os.environ.get('XROOTD_SERVER', 'root://dtn-eic.jlab.org'),
            'XROOTD_BASE_DIR': os.environ.get('XROOTD_BASE_DIR', '/volatile/eic/EPIC'),
        },
        'repo_dir': '/data/wenauseic/github/xrootd-mcp-server',
        'update_commands': [
            'export PATH=/eic/u/wenauseic/.nvm/versions/node/v22.17.0/bin:$PATH && cd /data/wenauseic/github/xrootd-mcp-server && git pull && npm install && npm run build',
        ],
    })

_github_token = os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN')
if _github_token:
    STDIO_MCP_SERVERS.append({
        'name': 'github',
        'source': 'github.com/github/github-mcp-server',
        'command': [
            '/data/wenauseic/github/github-mcp-server/github-mcp-server', 'stdio',
            '--toolsets=issues,pull_requests,actions,code_security,discussions',
        ],
        'env': {
            'GITHUB_PERSONAL_ACCESS_TOKEN': _github_token,
        },
        'repo_dir': '/data/wenauseic/github/github-mcp-server',
        'update_commands': [
            'cd /data/wenauseic/github/github-mcp-server && git pull && PATH=$PATH:/usr/local/go/bin go build -o github-mcp-server ./cmd/github-mcp-server',
        ],
    })

_zenodo_key = os.environ.get('ZENODO_API_KEY')
if _zenodo_key:
    STDIO_MCP_SERVERS.append({
        'name': 'zenodo',
        'source': 'github.com/eic/zenodo-mcp-server',
        'command': [
            os.environ.get('NODE_PATH', '/eic/u/wenauseic/.nvm/versions/node/v22.17.0/bin/node'),
            '/data/wenauseic/github/zenodo-mcp-server/build/src/index.js',
        ],
        'env': {
            'ZENODO_API_KEY': _zenodo_key,
        },
        'repo_dir': '/data/wenauseic/github/zenodo-mcp-server',
        'update_commands': [
            'export PATH=/eic/u/wenauseic/.nvm/versions/node/v22.17.0/bin:$PATH && cd /data/wenauseic/github/zenodo-mcp-server && git pull && npm install && npm run build',
        ],
    })

STDIO_MCP_SERVERS.append({
    'name': 'lxr',
    'source': 'github.com/BNLNPPS/lxr-mcp-server',
    'command': [
        os.path.join(os.environ.get('SWF_HOME', '/data/wenauseic/github'),
                     'swf-testbed/.venv/bin/python'),
        '/data/wenauseic/github/lxr-mcp-server/lxr_mcp_server.py',
    ],
    'env': {},
    'repo_dir': '/data/wenauseic/github/lxr-mcp-server',
    'update_commands': [
        'cd /data/wenauseic/github/lxr-mcp-server && git pull',
    ],
})

STDIO_MCP_SERVERS.append({
    'name': 'uproot',
    'source': 'github.com/eic/uproot-mcp-server',
    'command': [
        os.path.join(os.environ.get('SWF_HOME', '/data/wenauseic/github'),
                     'swf-testbed/.venv/bin/uproot-mcp-server'),
    ],
    'env': {},
    'repo_dir': '/data/wenauseic/github/uproot-mcp-server',
    'update_commands': [
        'cd /data/wenauseic/github/uproot-mcp-server && git pull && '
        + os.path.join(os.environ.get('SWF_HOME', '/data/wenauseic/github'),
                       'swf-testbed/.venv/bin/pip')
        + ' install -e ".[xrootd]"',
    ],
})

# Virtual tool definition for server management
BOT_MANAGE_SERVERS_TOOL = {
    "name": "bot_manage_servers",
    "description": (
        "List or update the bot's MCP servers. "
        "action='list' shows all servers and which are updatable. "
        "action='update', server_name='xrootd' pulls latest code, rebuilds, and restarts that server."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "update"],
                "description": "Action to perform.",
            },
            "server_name": {
                "type": "string",
                "description": "Server to update (required for action='update').",
            },
        },
        "required": ["action"],
    },
}

# ── ePIC Doc Search (virtual tools — handled in-process) ───────────────────

CHROMA_PATH = "/data/wenauseic/github/swf-monitor/chroma_db"
CHROMA_COLLECTION = "bamboo_docs"

EPIC_DOC_SEARCH_TOOL = {
    "name": "epic_doc_search",
    "description": (
        "Search ePIC documentation by natural-language query (semantic vector search). "
        "Covers SWF testbed, SWF monitor, Bamboo/PanDA, EICrecon, containers, ePIC production, "
        "EIC master docs, afterburner, eic-shell, and more. "
        "Use for conceptual 'how does X work?' questions about the software and experiment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language question (e.g. 'how does fast processing work?').",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results (default 5, max 20).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

EPIC_DOC_CONTENTS_TOOL = {
    "name": "epic_doc_contents",
    "description": (
        "Show what's in epicdoc — table of contents of all indexed ePIC documentation. "
        "Lists every source and document with chunk counts. Use to discover what documentation "
        "is searchable via epic_doc_search."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


class DocSearchHandler:
    """Handles epic_doc_search and epic_doc_contents using a ChromaDB vector store.

    Lazy-loads ChromaDB on first call, caches the collection handle.
    Runs in the long-lived bot process so the embedding model loads once.
    """

    def __init__(self):
        self._collection = None
        self._init_error = None

    def _ensure_collection(self):
        """Lazy-load ChromaDB collection. Returns error string or None."""
        if self._collection is not None:
            return None
        if self._init_error is not None:
            return self._init_error

        try:
            import chromadb
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        except ImportError:
            self._init_error = "chromadb not installed"
            return self._init_error

        if not os.path.exists(CHROMA_PATH):
            self._init_error = f"ChromaDB path not found: {CHROMA_PATH}"
            return self._init_error

        try:
            os.environ.setdefault("HF_HOME", "/opt/swf-monitor/shared/hf_cache")
            ef = SentenceTransformerEmbeddingFunction("all-MiniLM-L6-v2")
            client = chromadb.PersistentClient(path=CHROMA_PATH)
            self._collection = client.get_collection(CHROMA_COLLECTION, embedding_function=ef)
            logger.info(
                f"DocSearch: loaded collection '{CHROMA_COLLECTION}' "
                f"({self._collection.count()} chunks)"
            )
        except Exception as e:
            self._init_error = f"ChromaDB init failed: {e}"
            return self._init_error
        return None

    async def search(self, arguments: dict) -> str:
        """Handle epic_doc_search."""
        query = str(arguments.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query is required"})

        top_k = max(1, min(int(arguments.get("top_k", 5)), 20))

        err = await asyncio.to_thread(self._ensure_collection)
        if err:
            return json.dumps({"error": err})

        try:
            raw = await asyncio.to_thread(
                self._collection.query,
                query_texts=[query], n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            return json.dumps({"error": f"ChromaDB query failed: {e}"})

        results = []
        docs = (raw.get("documents") or [[]])[0]
        metas = (raw.get("metadatas") or [[]])[0]
        dists = (raw.get("distances") or [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            score = max(0, (1 - dist) * 100)
            results.append({
                "score": round(score),
                "source": meta.get("source", "?"),
                "file": meta.get("rel_path", "?"),
                "excerpt": doc[:1500],
            })
        return json.dumps({"query": query, "results": results})

    async def contents(self, arguments: dict) -> str:
        """Handle epic_doc_contents."""
        err = await asyncio.to_thread(self._ensure_collection)
        if err:
            return json.dumps({"error": err})

        try:
            all_meta = await asyncio.to_thread(
                self._collection.get, include=["metadatas"],
            )
        except Exception as e:
            return json.dumps({"error": f"ChromaDB get failed: {e}"})

        sources = {}
        for meta in all_meta.get("metadatas", []):
            src = meta.get("source", "unknown")
            rel = meta.get("rel_path", "?")
            total = meta.get("total_chunks", 1)
            if src not in sources:
                sources[src] = {}
            sources[src][rel] = total

        toc = {}
        total_chunks = 0
        total_files = 0
        for src, files in sorted(sources.items()):
            file_list = []
            for rel, chunks in sorted(files.items()):
                file_list.append({"file": rel, "chunks": chunks})
                total_chunks += chunks
                total_files += 1
            toc[src] = file_list

        return json.dumps({
            "summary": f"{total_files} documents, {total_chunks} chunks across {len(toc)} sources",
            "sources": toc,
        })


SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(__file__), 'system_prompt.txt')


def _load_system_preamble():
    """Read system prompt from file, fresh on every call."""
    try:
        with open(SYSTEM_PROMPT_FILE) as f:
            return f.read()
    except FileNotFoundError:
        return "You are the PanDA bot for the ePIC experiment."


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


class StdioMCPClient:
    """MCP client for subprocess-based servers using stdio (stdin/stdout JSON-RPC)."""

    def __init__(self, name: str, command: list, env: dict = None, args: list = None):
        self.name = name
        self.command = command + (args or [])
        self.env = {**os.environ, **(env or {})}
        self._request_id = 0
        self._process = None
        self.server_instructions = ""

    async def start(self):
        """Launch the subprocess."""
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        logger.info(f"Stdio MCP '{self.name}' started (pid {self._process.pid})")

    async def _request(self, method: str, params: dict | None = None):
        if not self._process or self._process.returncode is not None:
            raise RuntimeError(f"Stdio MCP '{self.name}' not running")
        self._request_id += 1
        req_id = self._request_id
        body = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            body["params"] = params
        line = json.dumps(body) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

        # Read lines until we get the response matching our request ID,
        # skipping any server notifications (no "id" field)
        while True:
            resp_line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=60
            )
            if not resp_line:
                raise RuntimeError(f"Stdio MCP '{self.name}' closed stdout")
            msg = json.loads(resp_line)
            if "id" in msg and msg["id"] == req_id:
                return msg
            # Skip notifications and other non-response messages
            logger.debug(f"Stdio MCP '{self.name}' skipped: {msg.get('method', '?')}")

    async def initialize(self):
        resp = await self._request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "panda-bot", "version": "1.0"},
        })
        self.server_instructions = (
            resp.get("result", {}).get("instructions", "")
        )
        # Send initialized notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._process.stdin.write((json.dumps(notif) + "\n").encode())
        await self._process.stdin.drain()
        return resp

    async def list_tools(self):
        result = await self._request("tools/list")
        return result.get("result", {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict):
        result = await self._request("tools/call", {
            "name": name, "arguments": arguments,
        })
        return result.get("result", {})

    async def close(self):
        if self._process and self._process.returncode is None:
            self._process.stdin.close()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            logger.info(f"Stdio MCP '{self.name}' stopped")


def mcp_tool_to_anthropic(tool):
    """Convert an MCP tool definition to Anthropic Messages API format."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool["inputSchema"],
    }


SELECT_TOOLS_TOOL = {
    "name": "select_tools",
    "description": (
        "Load additional tools by name from the tool catalog. "
        "Call this when the pre-loaded tools don't cover what you need. "
        "The tool catalog is listed in your system prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool names to load from the catalog.",
            },
        },
        "required": ["names"],
    },
}

# Number of tools to pre-load via semantic matching
TOP_K_TOOLS = 8


class ToolSelector:
    """Selects relevant tools for a user message via semantic similarity.

    At startup, embeds all tool descriptions into vectors. Tool names are
    prefixed with their MCP server name for embedding (e.g. "github:get_job_logs")
    so the model can distinguish tools from different domains. Returned names
    are unprefixed (the actual tool name for dispatch).
    """

    def __init__(self):
        self._model = SentenceTransformer('all-MiniLM-L6-v2')
        self._tool_names: list[str] = []
        self._tool_embeddings: np.ndarray | None = None

    def build_index(self, tool_registry: dict[str, dict], server_map: dict[str, str]):
        """Embed all tool descriptions with server-prefixed names.

        Args:
            tool_registry: tool_name → Anthropic tool dict
            server_map: tool_name → server name (e.g. 'github', 'xrootd', 'swf-monitor')
        """
        self._tool_names = []
        texts = []
        for name, tool in tool_registry.items():
            self._tool_names.append(name)
            server = server_map.get(name, 'unknown')
            texts.append(f"{server}:{name}: {tool['description']}")
        self._tool_embeddings = self._model.encode(texts, normalize_embeddings=True)
        logger.info(f"ToolSelector: indexed {len(self._tool_names)} tools")

    def select(self, message: str, top_k: int = TOP_K_TOOLS) -> list[tuple[str, float]]:
        """Return top-K tool names with scores, ranked by relevance."""
        if self._tool_embeddings is None or len(self._tool_names) == 0:
            return []
        msg_embedding = self._model.encode(message, normalize_embeddings=True)
        scores = self._tool_embeddings @ msg_embedding
        top_indices = np.argsort(scores)[-top_k:][::-1]
        return [(self._tool_names[i], float(scores[i])) for i in top_indices]


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
        self.system_prompt = _load_system_preamble()
        self.anthropic_tools = []
        self._tool_registry: dict[str, dict] = {}  # tool_name → Anthropic tool dict
        self._tool_server_map: dict[str, str] = {}  # tool_name → server name
        self._tool_router: dict[str, object] = {}  # tool_name → MCP client
        self._stdio_clients: list[StdioMCPClient] = []
        self._tool_selector = ToolSelector()
        self._doc_handler = DocSearchHandler()
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
        """System prompt — re-read from file on every message so edits take effect live."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        preamble = _load_system_preamble()
        instructions = getattr(self, '_server_instructions', '')
        prompt = preamble
        if instructions:
            prompt += "\n" + instructions
        return f"Current date and time: {now}\n\n{prompt}"

    @staticmethod
    async def _git_version(repo_dir):
        """Get short version string from a git repo: hash + date + tag if any."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'git', 'log', '-1', '--format=%h %ci',
                cwd=repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            parts = stdout.decode().strip().split(' ', 1)
            commit_hash = parts[0] if parts else '?'
            commit_date = parts[1][:10] if len(parts) > 1 else ''
            # Try to get a tag
            proc2 = await asyncio.create_subprocess_exec(
                'git', 'describe', '--tags', '--always',
                cwd=repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await proc2.communicate()
            tag = stdout2.decode().strip()
            return f"{tag} ({commit_date})" if tag != commit_hash else f"{commit_hash} ({commit_date})"
        except Exception:
            return 'unknown'

    async def _handle_manage_servers(self, arguments):
        """Handle the bot_manage_servers virtual tool."""
        action = arguments.get('action', 'list')

        if action == 'list':
            lines = [
                "POST THIS TABLE EXACTLY AS-IS — do not reformat or omit columns:",
                "",
                "| Server | Type | Version | Updatable |",
                "| --- | --- | --- | --- |",
            ]
            swf_ver = await self._git_version('/data/wenauseic/github/swf-monitor')
            lines.append(f"| swf-monitor | HTTP (PanDA, PCS, memory) | {swf_ver} | no |")
            for cfg in STDIO_MCP_SERVERS:
                ver = await self._git_version(cfg['repo_dir']) if cfg.get('repo_dir') else '?'
                upd = 'yes' if cfg.get('update_commands') else 'no'
                lines.append(f"| {cfg['name']} | stdio ({cfg.get('source', '')}) | {ver} | {upd} |")
            return '\n'.join(lines)

        if action == 'update':
            name = arguments.get('server_name', '')
            cfg = next((s for s in STDIO_MCP_SERVERS if s['name'] == name), None)
            if not cfg:
                return json.dumps({'error': f"Unknown server '{name}'"})
            if not cfg.get('update_commands'):
                return json.dumps({'error': f"Server '{name}' is not updatable"})

            # Run update commands
            output_lines = []
            for cmd in cfg['update_commands']:
                logger.info(f"Updating '{name}': {cmd}")
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                output_lines.append(stdout.decode().strip())
                if proc.returncode != 0:
                    return json.dumps({
                        'error': f"Update command failed (exit {proc.returncode})",
                        'output': '\n'.join(output_lines),
                    })

            # Restart the stdio server
            old_client = next((c for c in self._stdio_clients if c.name == name), None)
            if old_client:
                # Collect old tool names before removing routes
                old_tool_names = {
                    t for t, c in self._tool_router.items() if c is old_client
                }
                for t in old_tool_names:
                    del self._tool_router[t]
                self.anthropic_tools = [
                    t for t in self.anthropic_tools if t['name'] not in old_tool_names
                ]
                self._stdio_clients.remove(old_client)
                await old_client.close()

            # Start fresh
            try:
                client = StdioMCPClient(
                    name=cfg['name'],
                    command=cfg['command'],
                    env=cfg.get('env'),
                )
                await client.start()
                await client.initialize()
                tools = await client.list_tools()
                for t in tools:
                    self.anthropic_tools.append(mcp_tool_to_anthropic(t))
                    self._tool_router[t['name']] = client
                self._stdio_clients.append(client)
                return json.dumps({
                    'success': True,
                    'server': name,
                    'tools_count': len(tools),
                    'update_output': '\n'.join(output_lines),
                })
            except Exception as e:
                return json.dumps({'error': f"Restart failed: {e}"})

        return json.dumps({'error': f"Unknown action '{action}'"})

    @staticmethod
    def _generate_dpid():
        """Generate a short unique Data Provenance ID."""
        raw = f"{time.time():.6f}-{os.getpid()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:8].upper()

    async def _record_dpid(self, dpid, tool_name, tool_args):
        """Record a DPID to the database."""
        from monitor_app.models import DataProvenance
        try:
            await asyncio.to_thread(
                DataProvenance.objects.create,
                dpid=dpid, tool_name=tool_name, tool_args=tool_args,
            )
        except Exception:
            logger.exception(f"Failed to record DPID:{dpid}")

    async def _setup_mcp(self):
        """Discover tools from all MCP servers (HTTP + stdio).

        Builds a tool registry (full schemas) and a semantic index for
        progressive tool loading. Tools are selected per-message based
        on relevance rather than loaded all at once.
        """
        # 1. HTTP MCP server (Django — PanDA, PCS, memory tools)
        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()
            tools = await mcp.list_tools()
            for t in tools:
                if t["name"].startswith(BOT_TOOL_PREFIXES):
                    at = mcp_tool_to_anthropic(t)
                    self._tool_registry[at["name"]] = at
                    self._tool_server_map[at["name"]] = "swf-monitor"
            logger.info(f"HTTP MCP: {len(self._tool_registry)} tools")
            if mcp.server_instructions:
                self._server_instructions = mcp.server_instructions
        except Exception:
            logger.exception("Failed HTTP MCP setup — will retry on first message")
        finally:
            await mcp.close()

        # 2. Stdio MCP servers (xrootd, github, etc.)
        for server_cfg in STDIO_MCP_SERVERS:
            try:
                client = StdioMCPClient(
                    name=server_cfg['name'],
                    command=server_cfg['command'],
                    env=server_cfg.get('env'),
                    args=server_cfg.get('args'),
                )
                await client.start()
                await client.initialize()
                tools = await client.list_tools()
                for t in tools:
                    at = mcp_tool_to_anthropic(t)
                    self._tool_registry[at["name"]] = at
                    self._tool_server_map[at["name"]] = server_cfg['name']
                    self._tool_router[t["name"]] = client
                self._stdio_clients.append(client)
                logger.info(
                    f"Stdio MCP '{client.name}': {len(tools)} tools"
                )
            except Exception:
                logger.exception(
                    f"Failed to start stdio MCP '{server_cfg['name']}'"
                )

        # 3. Virtual tools (handled by the bot itself)
        self._tool_registry['bot_manage_servers'] = BOT_MANAGE_SERVERS_TOOL
        self._tool_registry['epic_doc_search'] = EPIC_DOC_SEARCH_TOOL
        self._tool_registry['epic_doc_contents'] = EPIC_DOC_CONTENTS_TOOL
        self._tool_server_map['epic_doc_search'] = 'epicdoc'
        self._tool_server_map['epic_doc_contents'] = 'epicdoc'

        # 3b. Pre-load ChromaDB so first doc query is instant
        err = self._doc_handler._ensure_collection()
        if err:
            logger.warning(f"DocSearch init deferred: {err}")

        # 4. Build semantic index with server-prefixed names for domain separation.
        # "github:get_job_logs" vs "panda:panda_list_jobs" gives the embedding
        # model semantic context to distinguish CI jobs from PanDA jobs.
        self._tool_selector.build_index(self._tool_registry, self._tool_server_map)

        logger.info(f"Total tools in registry: {len(self._tool_registry)}")

    def _build_tool_catalog(self):
        """One-liner catalog of all tools for the system prompt."""
        lines = [
            "TOOL AWARENESS — three tiers:",
            "1. CATALOG: All tools are in your system prompt as one-liners — full awareness at minimal token cost.",
            "2. PRE-LOADED: Tools you can call directly — pre-loaded based on relevance to this query.",
            "3. select_tools: Call this to load any catalog tool that isn't pre-loaded. You are never limited to pre-loaded tools.",
            "",
            "Full tool catalog:",
        ]
        for name, tool in sorted(self._tool_registry.items()):
            desc = tool["description"].split('\n')[0][:120]
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    # Stdio servers only included when the user's message mentions the domain.
    # Maps server name → keywords that trigger inclusion.
    _SERVER_KEYWORDS = {
        'github': ('github', 'repo', 'pr ', 'pull request', 'issue', 'commit', 'branch', 'discussion'),
        'xrootd': ('xrootd', 'file', 'storage', 'directory', 'volatile'),
        'zenodo': ('zenodo', 'record', 'doi', 'deposit'),
        'lxr': ('lxr', 'code browser', 'cross-reference', 'source code', 'identifier',
                'class definition', 'where is', 'defined', 'header file', 'algorithm'),
        'uproot': ('uproot', 'root file', '.root', 'ttree', 'branch', 'histogram',
                   'root data', 'hepdata', 'ntuple'),
    }

    def _extract_thread_tool_history(self, thread_context: str | None) -> tuple[set[str], set[str]]:
        """Extract tool history from prior bot replies in a thread.

        Parses (tools suggested: ...) and (tools used: ...) metadata lines
        from bot messages in the thread.

        Returns (prior_servers, prior_tools) where:
        - prior_servers: servers of tools actually used in prior turns
        - prior_tools: top 3 suggested tool names from each prior turn
        """
        prior_servers = set()
        prior_tools = set()
        if not thread_context:
            return prior_servers, prior_tools

        for line in thread_context.split('\n'):
            if not line.startswith('Bot:'):
                continue
            # Extract used tools → their servers
            used_match = re.search(r'\(tools used:\s*([^)]+)\)', line)
            if used_match and used_match.group(1).strip() != 'none':
                for tool_name in used_match.group(1).split(','):
                    tool_name = tool_name.strip()
                    server = self._tool_server_map.get(tool_name)
                    if server:
                        prior_servers.add(server)
            # Extract top 3 suggested tools (name:score format)
            sugg_match = re.search(r'\(tools suggested:\s*([^)]+)\)', line)
            if sugg_match:
                entries = sugg_match.group(1).split(',')
                for entry in entries[:3]:
                    name = entry.strip().split(':')[0]
                    if name and name != 'none':
                        prior_tools.add(name)

        return prior_servers, prior_tools

    def _select_tools_for_message(self, message: str, thread_context: str | None = None) -> tuple[list[dict], list[tuple[str, float]]]:
        """Pick tools for this message: semantic top-K + thread history + always-on tools.

        Tool set is built from three sources:
        1. All tools from servers used in prior thread turns
        2. Top 3 suggested tools from each prior thread turn
        3. Top-K from vector search on the current message

        Strips the [username in #channel] tag before embedding. Excludes
        stdio server tools unless activated by keyword or thread history.

        Returns (active_tools, scored_names) where scored_names is
        [(name, score), ...] in ranked order.
        """
        # Strip context tag before embedding
        clean_message = re.sub(r'^\[.*?\]\s*', '', message)
        msg_lower = clean_message.lower()

        # Thread history: servers used + top suggestions from prior turns
        prior_servers, prior_tools = self._extract_thread_tool_history(thread_context)

        # Determine which servers are relevant
        allowed_servers = {'swf-monitor', 'epicdoc'} | prior_servers
        for server, keywords in self._SERVER_KEYWORDS.items():
            if any(kw in msg_lower for kw in keywords):
                allowed_servers.add(server)

        # Start with prior suggested tools (carry forward from thread)
        tools = []
        scored = []
        seen = set()
        for name in prior_tools:
            if name in self._tool_registry and name not in seen:
                server = self._tool_server_map.get(name, 'unknown')
                if server in allowed_servers:
                    tools.append(self._tool_registry[name])
                    seen.add(name)

        # Add vector search results for current message
        all_scored = self._tool_selector.select(clean_message, top_k=TOP_K_TOOLS)
        for name, score in all_scored:
            server = self._tool_server_map.get(name, 'unknown')
            if server not in allowed_servers:
                continue
            if name in self._tool_registry and name not in seen:
                tools.append(self._tool_registry[name])
                scored.append((name, score))
                seen.add(name)
        # Always include virtual tools
        for name in ('bot_manage_servers',):
            if name in self._tool_registry and name not in seen:
                tools.append(self._tool_registry[name])
                seen.add(name)
        # Always include select_tools for fallback
        tools.append(SELECT_TOOLS_TOOL)
        return tools, scored

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
            reply, dpid_verified, tool_meta = await self._process_message(messages, tagged_message, root_id)
            # Strip any tool metadata Haiku echoed from conversation history
            reply = re.sub(r'\n*\*?\(tools (?:suggested|used):[^)]*\)\*?', '', reply)
            no_query_warn = ":warning: *This response was not based on a live data query.*"
            reply = reply.replace(no_query_warn, "").rstrip()
            if not dpid_verified and not reply.startswith("Sorry,"):
                reply += "\n\n" + no_query_warn
            # Append tool selection metadata only when tools were used
            if tool_meta['used']:
                suggested = ', '.join(tool_meta['suggested']) or 'none'
                used = ', '.join(tool_meta['used'])
                reply += f"\n\n*(tools suggested: {suggested})*\n*(tools used: {used})*"
            # Record inside lock so the next load sees this exchange
            await self._record_exchange(tagged_message, reply, post_id, root_id)

        await self._post_reply(reply, reply_channel, post_id, root_id)

    async def _render_plot(self, code):
        """Execute matplotlib code and return the PNG path, or None on failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plot_path = os.path.join(tmpdir, 'plot.png')
            # Rewrite savefig path to our temp location
            code = re.sub(
                r"plt\.savefig\([^)]+\)",
                f"plt.savefig('{plot_path}', dpi=150, bbox_inches='tight')",
                code,
            )
            # Ensure no plt.show()
            code = code.replace('plt.show()', '')
            # Prepend headless backend
            code = "import matplotlib\nmatplotlib.use('Agg')\n" + code

            script_path = os.path.join(tmpdir, 'plot.py')
            with open(script_path, 'w') as f:
                f.write(code)

            try:
                # Use sys.executable so the subprocess has the same venv
                import sys
                result = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, script_path],
                    capture_output=True, text=True, timeout=30,
                    cwd=tmpdir,
                )
                if result.returncode != 0:
                    logger.warning(f"Plot script failed: {result.stderr[:500]}")
                    return None
                if not os.path.exists(plot_path):
                    logger.warning("Plot script ran but produced no image")
                    return None
                # Copy out of tmpdir before it's cleaned up
                import shutil
                fd, final_path = tempfile.mkstemp(suffix='.png')
                os.close(fd)
                shutil.copy2(plot_path, final_path)
                return final_path
            except subprocess.TimeoutExpired:
                logger.warning("Plot script timed out")
                return None
            except Exception:
                logger.exception("Plot execution failed")
                return None

    async def _post_reply(self, reply, reply_channel, post_id, root_id):
        # Extract and render any plot code blocks (python-plot tag, or python with savefig)
        file_ids = []
        plot_match = re.search(r'```python-plot\s*\n(.*?)```', reply, re.DOTALL)
        if not plot_match:
            # Fallback: detect plain python blocks that contain plt.savefig
            m = re.search(r'```python\s*\n(.*?)```', reply, re.DOTALL)
            if m and 'plt.savefig' in m.group(1):
                plot_match = m
        if plot_match:
            plot_code = plot_match.group(1)
            logger.info("Detected plot code, rendering...")
            plot_path = await self._render_plot(plot_code)
            if plot_path:
                try:
                    uploaded = await asyncio.to_thread(
                        self.driver.files.upload_file,
                        reply_channel,
                        {'files': ('plot.png', open(plot_path, 'rb'))},
                    )
                    fid = uploaded['file_infos'][0]['id']
                    file_ids.append(fid)
                    logger.info(f"Plot uploaded: {fid}")
                    # Remove the code block from the message since we have the image
                    reply = reply.replace(plot_match.group(0), '*(plot attached)*')
                except Exception:
                    logger.exception("Failed to upload plot")
                finally:
                    try:
                        os.unlink(plot_path)
                    except OSError:
                        pass

        if len(reply) > MM_POST_LIMIT:
            reply = reply[:MM_POST_LIMIT - 20] + '\n\n... (truncated)'

        thread_root = root_id or post_id
        try:
            logger.info("Posting reply to Mattermost...")
            post_options = {
                'channel_id': reply_channel,
                'message': reply,
                'root_id': thread_root,
            }
            if file_ids:
                post_options['file_ids'] = file_ids
            await asyncio.to_thread(
                self.driver.posts.create_post,
                options=post_options,
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

        Returns (reply_text, dpid_verified).  dpid_verified is True only when
        a tool was called AND the LLM cited a matching DPID in its final reply.
        """
        # Build user message with full thread context if it's a reply
        user_content = message_text
        thread_context = None
        if root_id:
            thread_context = await self._build_thread_context(root_id)
            if thread_context:
                user_content = (
                    f"[Thread conversation so far:\n{thread_context}\n]\n"
                    f"New reply: {message_text}"
                )

        messages.append({"role": "user", "content": user_content})

        reply = "Sorry, I encountered an error processing your question."
        exchange_dpids = []  # DPIDs generated in this exchange

        mcp = MCPClient(self.mcp_url)
        try:
            await mcp.initialize()

            # Fallback: if registry is empty (setup failed), load eagerly
            if not self._tool_registry:
                tools = await mcp.list_tools()
                for t in tools:
                    if t["name"].startswith(BOT_TOOL_PREFIXES):
                        at = mcp_tool_to_anthropic(t)
                        self._tool_registry[at["name"]] = at

            # Select tools relevant to this message + thread history
            active_tools, scored = self._select_tools_for_message(message_text, thread_context)
            active_tool_names = {t['name'] for t in active_tools}
            suggested_names = [
                f"{name}:{score:.2f}" for name, score in scored
            ]
            tools_used = []
            logger.info(f"Selected {len(active_tools)} tools: {suggested_names}")

            system = self._build_system_prompt()
            tool_catalog = self._build_tool_catalog()
            system_with_catalog = f"{system}\n\n{tool_catalog}"

            for _round in range(MAX_TOOL_ROUNDS):
                response = await self.claude.beta.messages.create(
                    # DO NOT change model without user approval
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    cache_control={"type": "ephemeral"},
                    system=system_with_catalog,
                    tools=active_tools,
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
                    if block.name not in ('select_tools', 'bot_manage_servers'):
                        tools_used.append(block.name)
                    try:
                        # Virtual tools handled by the bot itself
                        if block.name == 'select_tools':
                            loaded = []
                            for tname in block.input.get('names', []):
                                if tname in self._tool_registry and tname not in active_tool_names:
                                    active_tools.append(self._tool_registry[tname])
                                    active_tool_names.add(tname)
                                    loaded.append(tname)
                            result_text = json.dumps({
                                'loaded': loaded,
                                'message': f"Loaded {len(loaded)} tools. They are now available for use.",
                            })
                            logger.info(f"select_tools loaded: {loaded}")
                        elif block.name == 'bot_manage_servers':
                            result_text = await self._handle_manage_servers(block.input)
                        elif block.name == 'epic_doc_search':
                            result_text = await self._doc_handler.search(block.input)
                        elif block.name == 'epic_doc_contents':
                            result_text = await self._doc_handler.contents(block.input)
                        else:
                            # Route to the correct MCP server
                            if block.name in self._tool_router:
                                result = await self._tool_router[block.name].call_tool(
                                    block.name, block.input
                                )
                            else:
                                result = await mcp.call_tool(block.name, block.input)
                            content = result.get("content", [])
                            result_text = ""
                            for item in content:
                                if isinstance(item, dict) and "text" in item:
                                    result_text += item["text"]
                        # Assign DPID and stamp the result
                        dpid = self._generate_dpid()
                        exchange_dpids.append(dpid)
                        await self._record_dpid(dpid, block.name, block.input)
                        result_text = f"[DPID:{dpid}]\n{result_text}"
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

        # Verify: did the LLM cite a DPID that was actually generated?
        dpid_verified = False
        if exchange_dpids:
            cited = set(re.findall(r'DPID:\s*([A-F0-9]{8})', reply))
            matched = cited & set(exchange_dpids)
            if matched:
                dpid_verified = True
                logger.info(f"DPID verified: {matched}")
            else:
                logger.warning(
                    f"Tool called but no valid DPID cited. "
                    f"Generated: {exchange_dpids}, cited: {cited}"
                )

        # Strip DPID citations from reply — user doesn't need to see them
        reply = re.sub(r'\s*\[?DPID:\s*[A-F0-9]{8}\]?\s*', '', reply).strip()

        # Deduplicate tools_used preserving order
        seen = set()
        tools_used = [t for t in tools_used if not (t in seen or seen.add(t))]

        tool_meta = {
            'suggested': suggested_names,
            'used': tools_used,
        }
        return reply, dpid_verified, tool_meta

"""JLab and BNL Rucio catalogs on the authenticated SWF MCP service.

The maintained rucio-eic MCP implementation is loaded twice under isolated
module names because it reads one catalog configuration at import time.  The
plain tool functions are then registered on swf-monitor's FastMCP instance
with catalog prefixes.  Credentials, token caches, and the BNL X509 proxy stay
on swf-testbed; remote MCP clients see only read-only catalog operations.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
from pathlib import Path
from types import ModuleType

from django.conf import settings

from monitor_app.mcp import mcp


_CATALOGS = {
    'jlab_rucio': {
        'description': (
            'JLab Rucio science-data catalog (normally scope epic, with '
            'path-like /RECO and /SIMU dataset names). Read-only.'),
        'env': {
            'RUCIO_URL': settings.RUCIO_JLAB_URL,
            'RUCIO_AUTH_TYPE': 'userpass',
            'RUCIO_ACCOUNT': settings.RUCIO_JLAB_ACCOUNT,
            'RUCIO_USERNAME': settings.RUCIO_JLAB_USERNAME,
            'RUCIO_PASSWORD': settings.RUCIO_JLAB_PASSWORD,
            'RUCIO_VO': '',
            'RUCIO_CA_BUNDLE': 'false',
            'TOKEN_FILE_PATH': settings.RUCIO_JLAB_TOKEN_FILE,
            'X509_USER_PROXY': '',
        },
    },
    'bnl_rucio': {
        'description': (
            'BNL Rucio PanDA production catalog (normally scope group.EIC). '
            'Use for output and log registration, rules, locks, replicas, '
            'and RSE diagnostics. Read-only.'),
        'env': {
            'RUCIO_URL': settings.RUCIO_BNL_URL,
            'RUCIO_AUTH_TYPE': 'x509',
            'RUCIO_ACCOUNT': settings.RUCIO_BNL_ACCOUNT,
            'RUCIO_USERNAME': '',
            'RUCIO_PASSWORD': '',
            'RUCIO_VO': settings.RUCIO_BNL_VO,
            'RUCIO_CA_BUNDLE': settings.RUCIO_BNL_CA_BUNDLE,
            'TOKEN_FILE_PATH': settings.RUCIO_BNL_TOKEN_FILE,
            'X509_USER_PROXY': settings.RUCIO_BNL_X509_PROXY,
        },
    },
}


def _load_catalog(prefix: str, values: dict[str, str]) -> ModuleType:
    """Load one independently configured copy of the Rucio MCP module."""
    path = Path(settings.RUCIO_MCP_MODULE_PATH)
    if not path.is_file():
        raise RuntimeError(f'Rucio MCP module not found: {path}')

    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update({key: str(value) for key, value in values.items()})
        spec = importlib.util.spec_from_file_location(
            f'_swf_monitor_{prefix}', path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f'Cannot load Rucio MCP module: {path}')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _register_catalog(prefix: str, config: dict) -> list[str]:
    module = _load_catalog(prefix, config['env'])
    registered = []
    # Mirror the complete upstream service. This is deliberately discovery-
    # driven rather than a local tool allowlist; new Rucio MCP capabilities
    # appear here automatically after that maintained service is updated.
    upstream_tools = module.mcp._tool_manager.list_tools()
    for upstream_tool in upstream_tools:
        function_name = upstream_tool.name
        source = getattr(module, function_name)
        tool_name = f'{prefix}_{function_name}'

        async def wrapper(_source=source, **kwargs):
            return await asyncio.to_thread(_source, **kwargs)

        wrapper.__name__ = tool_name
        wrapper.__doc__ = inspect.getdoc(source) or ''
        wrapper.__signature__ = inspect.signature(source)
        source_description = upstream_tool.description or (
            wrapper.__doc__.splitlines()[0] if wrapper.__doc__ else '')
        description = f"{config['description']} {source_description}".strip()
        mcp.tool(
            name=tool_name,
            description=description,
        )(wrapper)
        RUCIO_TOOL_DISCOVERY.append({
            'name': tool_name,
            'description': description,
            'parameters': list(inspect.signature(source).parameters),
        })
        registered.append(tool_name)
    return registered


RUCIO_TOOL_DISCOVERY = []
RUCIO_TOOL_NAMES = []
for _prefix, _config in _CATALOGS.items():
    RUCIO_TOOL_NAMES.extend(_register_catalog(_prefix, _config))


def get_rucio_tool_discovery() -> list[dict]:
    """Discovery records for swf_list_available_tools."""
    return [dict(record) for record in RUCIO_TOOL_DISCOVERY]

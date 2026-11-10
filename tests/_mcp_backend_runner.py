"""Standalone backend application for the MCP integration test.

Run with: ``python -m uvicorn tests._mcp_backend_runner:app --port <port>`` with ``ADMINDB_URL``
pointing at an ephemeral PostgreSQL. Uses the real task catalog store and the default engine
registry (real PostgreSQL adapter).
"""

import os

from gui.backend.app.main import create_app
from sqlalchemy import create_engine

from dbarena.catalog.store import TaskCatalogStore

_admin_url = os.environ["ADMINDB_URL"]
_engine = create_engine(_admin_url)

app = create_app(catalog=TaskCatalogStore(_engine), registry=None, admin_url=_admin_url)

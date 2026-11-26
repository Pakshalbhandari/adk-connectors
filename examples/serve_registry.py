"""Connector registration API.  uvicorn examples.serve_registry:app --port 8100"""

from adk_connectors.api import create_app

from examples.shared import build_registry

app = create_app(build_registry())

"""Conventional ``python app.py`` entry point for the HTTP service."""

from turnbox.server import build_server, create_handler, serve
from turnbox.service import TurnBoxService, create_service, sample_service

__all__ = [
    "build_server",
    "create_handler",
    "serve",
    "TurnBoxService",
    "create_service",
    "sample_service",
]

if __name__ == "__main__":
    import os

    serve(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        db_path=os.environ.get("TURNBOX_DB", "data/turnbox.json"),
        load_samples=os.environ.get("TURNBOX_LOAD_SAMPLES", "1") == "1",
    )

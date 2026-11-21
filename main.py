"""Vercel entry point. Locally run `uv run uvicorn procurement.api:app --reload` as before.

On Vercel the private data bundle is unpacked first (procurement/cloud.py), then the app is imported.
"""

from procurement import cloud

cloud.prepare()

from procurement.api import app  # noqa: E402

cloud.patch()

__all__ = ["app"]

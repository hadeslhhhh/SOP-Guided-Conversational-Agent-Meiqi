"""Shared pytest configuration for offline and API-backed test suites."""

import os
import pytest


def pytest_collection_modifyitems(config, items):
    if os.getenv("OPENAI_API_KEY"):
        return
    skip_api = pytest.mark.skip(reason="requires OPENAI_API_KEY")
    for item in items:
        if "api" in item.keywords:
            item.add_marker(skip_api)

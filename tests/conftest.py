"""Shared test fixtures. No real Postgres/Twilio/OpenAI calls in the test
suite - everything is mocked, so tests are fast and deterministic."""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("CONVERSATION_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.tenants import Tenant  # noqa: E402


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        client_id="kapper_devries",
        business_name="Kapsalon De Vries",
        niche="kapper",
        twilio_number="+3234000001",
        calendar_type="none",
        calendar_config={},
        system_prompt_extra="Wees warm en informeel.",
        escalation_contact="+3247000001",
        escalation_email="eigenaar@kapsalondevries.test",
    )


class FakeConnection:
    """Minimal asyncpg connection mock with a fetchrow/fetch/execute
    interface, preprogrammed via return_value/side_effect."""

    def __init__(self):
        self.execute = AsyncMock(return_value="INSERT 0 1")
        self.fetchrow = AsyncMock(return_value=None)
        self.fetch = AsyncMock(return_value=[])
        self.fetchval = AsyncMock(return_value=None)


class FakePool:
    def __init__(self, connection: FakeConnection | None = None):
        self.connection = connection or FakeConnection()

    def acquire(self):
        return _AcquireContext(self.connection)


class _AcquireContext:
    def __init__(self, connection: FakeConnection):
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()

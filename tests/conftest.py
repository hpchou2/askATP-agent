"""
tests/conftest.py — shared fixtures.

Each test gets a fresh SQLite database seeded relative to today, so the
deterministic tools can be tested without an API key or the LLM.
"""

import os
import sqlite3
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import setup_db
from tools import sap_tools


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Seeds a temporary database and points the SAP tools at it."""
    path = tmp_path / "sap_mock.db"
    conn = sqlite3.connect(path)
    setup_db.create_tables(conn)
    setup_db.seed_data(conn, date.today())
    conn.close()
    monkeypatch.setattr(sap_tools, "DB_PATH", str(path))
    return path


def day(n: int) -> str:
    """Date n days from today, as YYYY-MM-DD."""
    return (date.today() + timedelta(days=n)).isoformat()

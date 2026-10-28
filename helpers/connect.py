"""Snowflake session for the quickstart. Uses the [default] entry of ~/.snowflake/connections.toml. Never logs credentials."""
from __future__ import annotations


# Every quickstart Snowflake session runs as this role; sql/setup_snowflake.sql creates it.
DEFAULT_ROLE = "OSSIE_QS_ROLE"


def connect(role: str | None = None):
    import snowflake.connector as sc

    # Explicit: with only role= given, the connector would not load the [default] entry.
    return sc.connect(connection_name="default", role=role or DEFAULT_ROLE)

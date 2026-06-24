import os
import re
import sqlite3
from pathlib import Path
import string
import pandas as pd
import aiosqlite

DB_PATH = Path(
    os.getenv(
        "ORDERS_DB_PATH",
        str(Path(__file__).parent / "data" / "orders.db"),
    )
)

ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    order_date TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL
);
"""

SCHEMA_DESCRIPTION = """Table: orders
Columns:
- order_id (TEXT, PRIMARY KEY): unique order identifier
- customer_id (TEXT): alphanumeric customer identifier
- order_date (TEXT): ISO 8601 date string (YYYY-MM-DD)
- amount (REAL): order amount in USD (normalized by ETL)
- currency (TEXT): currency code; always 'USD' after ETL normalization

Notes:
- There is only one table: orders.
- All amounts are already converted to USD.
- Use SQLite date functions on order_date (stored as TEXT in YYYY-MM-DD format).
- For "last N days", anchor to MAX(order_date) in the dataset unless the question specifies an absolute date range.
"""

_FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter",
    "create", "attach", "detach", "pragma", "vacuum", "replace"
}


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(ORDERS_SCHEMA)


def load_dataframe(df: pd.DataFrame) -> None:
    init_db()
    export_df = df.copy()
    export_df["order_date"] = export_df["order_date"].astype(str)
    with get_connection() as conn:
        conn.execute("DELETE FROM orders")
        export_df.to_sql("orders", conn, if_exists="append", index=False)


def _normalize_tokens(sql: str) -> list[str]:
    return [token.strip(string.punctuation) for token in sql.lower().split()]

def validate_sql(sql: str) -> None:
    cleaned = sql.strip().rstrip(";")
    if not cleaned:
        raise ValueError("SQL query is empty")
    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not allowed")
    if not cleaned.lower().startswith("select"):
        raise ValueError("Only SELECT queries are allowed")
    if any(token in _FORBIDDEN for token in _normalize_tokens(cleaned)):
        raise ValueError("Query contains forbidden SQL keywords")


async def execute_query(sql: str) -> list[dict]:
    validate_sql(sql)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Use aiosqlite for async DB access
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        async with conn.execute(sql) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

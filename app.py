import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta, date
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

import db
from nl_sql import SqlGenerationError, UnsupportedQuestionError, ask_question, ask_question_with_agent
import semantic_search

logger = logging.getLogger(__name__)
log_path = str(Path(__file__).parent / "data" / "my_app.log")

logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

CSV_PATH = Path(
    os.getenv(
        "ORDERS_CSV_PATH",
        str(Path(__file__).parent / "data" / "orders_cleaned.csv"),
    )
)

orders_df: pd.DataFrame | None = None
latest_order_date = None


class Order(BaseModel):
    order_id: str
    customer_id: str
    order_date: date
    amount: float
    currency: str


class StatsResponse(BaseModel):
    total_revenue: float
    avg_order_value: float
    orders_per_day: dict[str, int]


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)


class AskResponse(BaseModel):
    answer: str
    sql_used: str
    rows: list[dict[str, Any]]


class SemanticSearchResult(BaseModel):
    order_id: str
    customer_id: str
    amount_usd: float
    order_date: date
    score: float


def load_orders() -> pd.DataFrame:
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Orders CSV not found at {CSV_PATH}")

    df = pd.read_csv(CSV_PATH, dtype={"order_id": str, "customer_id": str})
    df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce").dt.date
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.dropna(subset=["order_date"])
    if df.empty:
        raise ValueError(f"No valid orders found in {CSV_PATH}")
    return df


def reload_orders() -> None:
    global orders_df, latest_order_date

    df = load_orders()
    orders_df = df
    latest_order_date = df["order_date"].max()
    db.load_dataframe(df)
    semantic_search.rebuild_index(df, CSV_PATH)
    logger.info(
        "Loaded %s orders from %s into SQLite at %s (latest date: %s)",
        len(df),
        CSV_PATH,
        db.DB_PATH,
        latest_order_date,
    )


def get_orders_df() -> pd.DataFrame:
    if orders_df is None:
        raise HTTPException(status_code=503, detail="Orders data not loaded")
    return orders_df


def get_latest_order_date():
    if latest_order_date is None:
        raise HTTPException(status_code=503, detail="Orders data not loaded")
    return latest_order_date


@asynccontextmanager
async def lifespan(app: FastAPI):
    reload_orders()
    yield


app = FastAPI(title="Orders API", lifespan=lifespan)


def row_to_order(row: pd.Series) -> Order:
    return Order(
        order_id=str(row["order_id"]),
        customer_id=str(row["customer_id"]),
        order_date=row["order_date"],
        amount=round(float(row["amount"]), 2),
        currency=str(row["currency"]),
    )


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/readyz", response_class=PlainTextResponse)
def readyz() -> str:
    if orders_df is None or orders_df.empty:
        raise HTTPException(status_code=503, detail="Orders data not loaded")
    if not semantic_search.is_ready():
        raise HTTPException(status_code=503, detail="Semantic search index not ready")
    return "ok"


@app.get("/orders/customer/{customer_id}", response_model=list[Order])
def get_orders_by_customer(customer_id: str) -> list[Order]:
    df = get_orders_df()
    subset = df[df["customer_id"] == customer_id]
    return [row_to_order(row) for _, row in subset.iterrows()]


@app.get("/orders/stats", response_model=StatsResponse)
def get_order_stats() -> StatsResponse:
    df = get_orders_df()
    total_revenue = round(float(df["amount"].sum()), 2)
    order_count = len(df)
    avg_order_value = round(total_revenue / order_count, 2) if order_count else 0.0

    counts = df.groupby(df["order_date"].astype(str)).size().sort_index()
    orders_per_day = {date: int(count) for date, count in counts.items()}

    return StatsResponse(
        total_revenue=total_revenue,
        avg_order_value=avg_order_value,
        orders_per_day=orders_per_day,
    )


@app.get("/orders/recent", response_model=list[Order])
def get_recent_orders(
    days: int = Query(..., ge=1, description="Number of days to look back"),
) -> list[Order]:
    df = get_orders_df()
    cutoff = get_latest_order_date() - timedelta(days=days - 1)
    subset = df[df["order_date"] >= cutoff].sort_values("order_date", ascending=False)
    return [row_to_order(row) for _, row in subset.iterrows()]


@app.get("/orders/semantic_search", response_model=list[SemanticSearchResult])
def semantic_order_search(
    q: str = Query(..., min_length=1, description="Free-text semantic query"),
    top_k: int = Query(5, ge=1, le=100, description="Number of results to return"),
) -> list[SemanticSearchResult]:
    df = get_orders_df()
    semantic_search.ensure_index(df, CSV_PATH)
    logger.info("Semantic search query is %s", q.strip())
    logger.info("Semantic search top_k is %s", top_k)
    try:
        results = semantic_search.search(q.strip(), top_k)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return [SemanticSearchResult(**result) for result in results]


@app.post("/orders/ask", response_model=AskResponse)
async def orders_ask(request: AskRequest) -> AskResponse:
    try:
        # result = await ask_question(request.question.strip())
        result = await ask_question_with_agent(request.question.strip())
    except UnsupportedQuestionError as exc:
        logger.error(f"Ask request failed: Unsupported question: {exc.message}")
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except RuntimeError as exc:
        logger.error("Ask request failed: RuntimeError %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SqlGenerationError as exc:
        logger.error("Ask request failed: SqlGenerationError %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    token_count = result.pop("token_count")
    logger.info(
        "Ask request answered: question=%r sql=%r token_count=%s",
        request.question,
        result["sql_used"],
        token_count,
    )
    return AskResponse(**result)

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_NAME = os.getenv(
    "SEMANTIC_EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)
INDEX_DIR = Path(
    os.getenv(
        "SEMANTIC_INDEX_DIR",
        str(Path(__file__).parent / "data" / "semantic_index"),
    )
)
FAISS_INDEX_FILE = "orders.faiss"
RECORDS_FILE = "records.json"
MANIFEST_FILE = "manifest.json"


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    customer_id: str
    amount_usd: float
    order_date: str

    def to_result(self, score: float) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "amount_usd": self.amount_usd,
            "order_date": self.order_date,
            "score": round(score, 4),
        }


@dataclass
class _IndexState:
    index: faiss.IndexFlatIP | None = None
    records: tuple[OrderRecord, ...] = ()
    model: SentenceTransformer | None = None


_lock = threading.Lock()
_rebuild_lock = threading.Lock()
_state = _IndexState()


def format_order_text(row: pd.Series) -> str:
    order_date = row["order_date"]
    if hasattr(order_date, "isoformat"):
        order_date = order_date.isoformat()
    return (
        f"customer {row['customer_id']}, "
        f"${float(row['amount']):.2f} USD, "
        f"{order_date}"
    )


def _normalize_date(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _records_from_df(df: pd.DataFrame) -> tuple[OrderRecord, ...]:
    records: list[OrderRecord] = []
    for _, row in df.iterrows():
        records.append(
            OrderRecord(
                order_id=str(row["order_id"]),
                customer_id=str(row["customer_id"]),
                amount_usd=round(float(row["amount"]), 2),
                order_date=_normalize_date(row["order_date"]),
            )
        )
    return tuple(records)


def _get_model() -> SentenceTransformer:
    with _lock:
        if _state.model is None:
            logger.info("Loading embedding model %s", EMBEDDING_MODEL_NAME)
            _state.model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        return _state.model


def _write_manifest(csv_path: Path, row_count: int) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "csv_path": str(csv_path.resolve()),
        "csv_mtime": csv_path.stat().st_mtime if csv_path.exists() else 0,
        "row_count": row_count,
        "model": EMBEDDING_MODEL_NAME,
    }
    (INDEX_DIR / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2))


def _read_manifest() -> dict[str, Any] | None:
    manifest_path = INDEX_DIR / MANIFEST_FILE
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text())


def is_index_stale(csv_path: Path, row_count: int) -> bool:
    manifest = _read_manifest()
    if manifest is None:
        return True
    if manifest.get("model") != EMBEDDING_MODEL_NAME:
        return True
    if manifest.get("row_count") != row_count:
        return True
    if not csv_path.exists():
        return True
    return manifest.get("csv_mtime") != csv_path.stat().st_mtime


def _persist_index(
    index: faiss.IndexFlatIP,
    records: tuple[OrderRecord, ...],
    csv_path: Path,
) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_DIR / FAISS_INDEX_FILE))
    (INDEX_DIR / RECORDS_FILE).write_text(
        json.dumps([record.__dict__ for record in records], indent=2)
    )
    _write_manifest(csv_path, len(records))


def _load_index_from_disk() -> bool:
    index_path = INDEX_DIR / FAISS_INDEX_FILE
    records_path = INDEX_DIR / RECORDS_FILE
    if not index_path.exists() or not records_path.exists():
        return False

    loaded_index = faiss.read_index(str(index_path))
    raw_records = json.loads(records_path.read_text())
    records = tuple(
        OrderRecord(
            order_id=str(item["order_id"]),
            customer_id=str(item["customer_id"]),
            amount_usd=round(float(item["amount_usd"]), 2),
            order_date=str(item["order_date"]),
        )
        for item in raw_records
    )

    with _lock:
        _state.index = loaded_index
        _state.records = records

    logger.info("Loaded semantic index from disk with %s orders", len(records))
    return True


def _embed_orders(df: pd.DataFrame) -> tuple[faiss.IndexFlatIP, tuple[OrderRecord, ...]]:
    texts = [format_order_text(row) for _, row in df.iterrows()]
    model = _get_model()
    embeddings = model.encode(
        texts,
        batch_size=64,
        normalize_embeddings=True,
    )
    vectors = np.asarray(embeddings, dtype=np.float32)
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    return index, _records_from_df(df)


def _swap_index(index: faiss.IndexFlatIP, records: tuple[OrderRecord, ...]) -> None:
    with _lock:
        _state.index = index
        _state.records = records


def rebuild_index(
    df: pd.DataFrame,
    csv_path: Path | None = None,
    *,
    force: bool = False,
) -> None:
    """Build embeddings and swap the in-memory FAISS index atomically."""
    if df.empty:
        logger.warning("Skipping semantic index rebuild: empty dataframe")
        with _lock:
            _state.index = None
            _state.records = ()
        return

    with _rebuild_lock:
        if (
            not force
            and csv_path is not None
            and not is_index_stale(csv_path, len(df))
            and _load_index_from_disk()
        ):
            return

        logger.info("Building semantic index for %s orders", len(df))
        index, records = _embed_orders(df)
        _swap_index(index, records)

        if csv_path is not None:
            _persist_index(index, records, csv_path)

        logger.info("Semantic index rebuilt with %s orders", len(records))


def ensure_index(df: pd.DataFrame, csv_path: Path) -> None:
    """Load or rebuild the index if it is missing or stale."""
    with _lock:
        has_index = _state.index is not None and len(_state.records) > 0

    if has_index and not is_index_stale(csv_path, len(df)):
        return

    rebuild_index(df, csv_path)


def is_ready() -> bool:
    with _lock:
        return _state.index is not None and len(_state.records) > 0


def search(query: str, top_k: int) -> list[dict[str, Any]]:
    with _lock:
        index = _state.index
        records = _state.records

    if index is None or not records:
        raise RuntimeError("Semantic search index is not ready")

    model = _get_model()
    query_vector = model.encode(
        [query.strip()],
        normalize_embeddings=True,
    )
    vectors = np.asarray(query_vector, dtype=np.float32)
    k = min(top_k, len(records))
    scores, indices = index.search(vectors, k)

    results: list[dict[str, Any]] = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        results.append(records[idx].to_result(float(score)))
    return results

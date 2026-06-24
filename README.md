# Orders ETL + Query API

SAP CXII technical exercise: CSV ETL pipeline, FastAPI query service, Docker/Kubernetes deployment, and a natural-language SQL endpoint.

## Setup

```bash
cd sap
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Run ETL

```bash
python etl.py load data/orders.csv
```

This writes `orders_cleaned.csv` and loads the `orders` table into SQLite at `data/orders.db`.

### 2. Run API

```bash
export OPENROUTER_API_KEY=sk-or-...   # required for POST /orders/ask
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Environment variables:


| Variable             | Default                   | Description                |
| -------------------- | ------------------------- | -------------------------- |
| `ORDERS_CSV_PATH`    | `data/orders_cleaned.csv` | Cleaned CSV path           |
| `ORDERS_DB_PATH`     | `data/orders.db`          | SQLite database path       |
| `OPENROUTER_API_KEY` | *(none)*                  | Required for `/orders/ask` |
| `OPENROUTER_MODEL`   | `openai/gpt-4o-mini`      | OpenRouter model id        |
| `SEMANTIC_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model for semantic search |
| `SEMANTIC_INDEX_DIR` | `data/semantic_index`     | On-disk FAISS index directory |


## API Endpoints

- `GET /healthz` — liveness probe
- `GET /readyz` — readiness probe (data + semantic index loaded)
- `GET /orders/customer/{customer_id}` — orders for a customer
- `GET /orders/stats` — revenue stats
- `GET /orders/recent?days=N` — orders in the last N days (anchored to max date in dataset)
- `GET /orders/semantic_search?q=...&top_k=5` — semantic order search
- `POST /orders/ask` — natural-language query via LLM → SQL

Example semantic search:

```bash
curl "http://localhost:8000/orders/semantic_search?q=high+value+recent+orders&top_k=5"
```

Example NL query:

```bash
curl -X POST http://localhost:8000/orders/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "What is the total revenue from customer DK-13375 in the last 30 days?"}'
```

## Docker

```bash
docker build -t orders-api:latest .
docker run --rm -p 8000:8000 -e OPENROUTER_API_KEY=sk-or-... orders-api:latest
```

## Kubernetes

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

Provide `OPENROUTER_API_KEY` via a Kubernetes Secret (not ConfigMap) and reference it in the Deployment before using `/orders/ask` in cluster.

---

## Part 4a — Natural Language Query Layer

### Model and provider choice

**Provider:** [OpenRouter](https://openrouter.ai/)  
**Model:** `openai/gpt-4o-mini` (via OpenRouter)

**Why:** OpenRouter provides a unified API over many models (including OpenAI) with a simple Python SDK, so we can swap models via `OPENROUTER_MODEL` without changing application code. It avoids direct OpenAI billing/setup lock-in and works well for NL→SQL where `gpt-4o-mini` is fast and inexpensive. The SDK uses a familiar chat interface (`client.chat.send`).  
  
I chose gpt-4o-mini for the text-to-SQL feature because it offers the best trade-off between cost, latency, and schema-following quality for this workload. The application sends a compact schema context and requires structured JSON output, which aligns well with gpt-4o-mini’s strengths in fast, focused tasks and structured outputs. Larger models such as gpt-4o and Claude 3.5 Sonnet can improve accuracy on harder queries, but they increase inference cost and are not necessary for the majority of our order analytics questions. For this reason, gpt-4o-mini provides the most practical production balance for our use case.**

### System prompt template

```
You are a SQL assistant for an order analytics database.

Table: orders
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

Rules:
1. Answer ONLY with a single SQLite SELECT statement, or with UNSUPPORTED: <reason>.
2. Do not use markdown, code fences, or explanations.
3. Only query the orders table and only reference columns listed above.
4. If the question asks about data that is not represented in the schema (for example product categories, shipping status, or customer names), respond with UNSUPPORTED: <clear reason>.
5. Amounts are stored in the amount column (USD). Do not invent columns like revenue or price unless aliasing amount in SELECT.
6. For "last N days", filter relative to (SELECT MAX(order_date) FROM orders).
7. Use standard SQLite syntax.
```

### Retry loop example

**Question:** `Show me most recent 5 orders by combining customer ID with the currency using the CONCAT function.`

**Bad SQL (attempt 1):**

```sql
SELECT CONCAT(customer_id, currency) FROM orders order_date >= DATE(NOW(), '-5 days') limit 5
```

**Error:**

```
Failed with error: near ">=": syntax error
```

**Corrected SQL (attempt 2 after error appended to prompt):**

```sql
SELECT order_id, customer_id, order_date, amount, currency FROM orders WHERE order_date >= DATE((SELECT MAX(order_date) FROM orders), '-5 days') ORDER BY order_date DESC LIMIT 5
```

**Response shape:**

```json
{
"answer":"Found 5 orders.",
"sql_used":"SELECT order_id, customer_id, order_date, amount, currency FROM orders WHERE order_date >= DATE((SELECT MAX(order_date) FROM orders), '-5 days') ORDER BY order_date DESC LIMIT 5",
"rows":[{"order_id":"115427","customer_id":"EB-13975","order_date":"2027-12-30","amount":34.62,"currency":"USD"},{"order_id":"126221","customer_id":"CC-12430","order_date":"2027-12-30","amount":209.3,"currency":"USD"},{"order_id":"143259","customer_id":"PO-18865","order_date":"2027-12-30","amount":466.84,"currency":"USD"},{"order_id":"156720","customer_id":"JM-15580","order_date":"2027-12-30","amount":3.02,"currency":"USD"},{"order_id":"118885","customer_id":"JG-15160","order_date":"2027-12-29","amount":695.94,"currency":"USD"}]
}
```

### Implementation notes

- Schema context is injected into the system prompt from `db.SCHEMA_DESCRIPTION`.
- Invalid SQL or runtime errors trigger **one retry** with the error message appended to the user prompt.
- Questions about unavailable concepts return **400** when the model responds with `UNSUPPORTED: ...`.
- Each request logs the full prompt messages, generated SQL, and token count.

---

## Part 4b — Semantic Order Search

### Endpoint

`GET /orders/semantic_search?q=high+value+recent+orders&top_k=5`

Returns the top-k orders ranked by cosine similarity to the query:

```json
[
  {
    "order_id": "100090",
    "customer_id": "EB-13705",
    "amount_usd": 699.19,
    "order_date": "2024-07-08",
    "score": 0.4123
  }
]
```

Each order is embedded as a short text record, for example:

`customer EB-13705, $699.19 USD, 2024-07-08`

At query time the free-text query is embedded with the same model and matched against the FAISS index using cosine similarity (`IndexFlatIP` over L2-normalized vectors).

### Embedding model choice

**Model:** `sentence-transformers/all-MiniLM-L6-v2`

**Why this model fits short structured-text records:**

- It is a compact sentence embedding model (~22M parameters, 384 dimensions) optimized for semantic similarity rather than long-form generation.
- It performs well on short factual strings like `"customer C001, $320 USD, 2024-03-15"`, which are closer to sentence-level retrieval than document search.
- Latency and memory footprint are low enough to embed ~5k orders at startup and serve interactive search on a single API instance.
- It is widely used for semantic retrieval benchmarks and integrates cleanly through the `sentence-transformers` library.

Larger models (e.g. `all-mpnet-base-v2`) can improve ranking quality but increase startup time and RAM; for this exercise dataset size, MiniLM is the better latency/cost trade-off.

### Index storage: FAISS vs in-memory NumPy

**Chosen approach:** FAISS `IndexFlatIP` with on-disk persistence under `data/semantic_index/`.

| Approach | Pros | Cons |
| -------- | ---- | ---- |
| **FAISS (chosen)** | Exact top-k search, fast C++ backend, easy persistence via `faiss.write_index`, scales to much larger catalogs | Extra dependency (`faiss-cpu`) |
| **In-memory NumPy** | Minimal dependencies, easy to read | Pure Python/C array loop for top-k; no built-in persistence; slower as catalog grows |

For ~5k orders, both would work, but FAISS gives a clearer production path while keeping query latency predictable.

### Index rebuild behavior

The index is rebuilt in three places:

1. **API startup** — `reload_orders()` loads CSV/SQLite and calls `semantic_search.rebuild_index(...)`.
2. **ETL completion** — `python etl.py load ...` writes cleaned CSV/SQLite and rebuilds the on-disk FAISS index with `force=True`.
3. **First search after stale data** — `ensure_index()` compares a manifest (`csv_mtime`, row count, model name) and rebuilds if the CSV changed.

**Non-blocking rebuild strategy:**

- Search requests loads the current in-memory index reference and query it without holding a lock for embedding/search.
- Rebuilds take a module-level `_rebuild_lock` to ensure that only one thread can trigger a heavy index generation at any given time.
- A new index is built completely in local variables, then swapped into shared state under a short lock. In-flight searches against the previous index object continue safely until the swap completes.




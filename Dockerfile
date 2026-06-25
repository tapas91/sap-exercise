FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app.py etl.py db.py nl_sql.py semantic_search.py ./
COPY data/orders_cleaned.csv /data/orders_cleaned.csv

RUN mkdir -p /app/data
RUN touch /app/data/my_app.log
RUN chown -R appuser:appuser /app /data

USER appuser

ENV ORDERS_CSV_PATH=/data/orders_cleaned.csv
ENV ORDERS_DB_PATH=/data/orders.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

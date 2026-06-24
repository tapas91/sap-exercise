import csv
from datetime import datetime
from pathlib import Path
import sys
import os
import semantic_search
import pandas as pd

import db

def read_orders(csv_path):
    """Read raw order data from csv_path and return a list of raw row dicts."""
    data = []
    with open(csv_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            data.append(row)
    return data

def transform_orders(raw_orders):
    """
    Normalize date and currency fields, handle missing values,
    and return a list of standardized order dicts.
    """
    CURRENCY_RATE = {
        "EUR": 1.1,
        "USD": 1
    }

    def normalize_date(date_str):
        if not date_str:
            return None
        date_str = date_str.strip()
        # Try multiple common date formats
        formats = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    transformed = []
    for row in raw_orders:
        order_id = row.get('order_id', '').strip()
        customer_id = row.get('customer_id', '').strip()
        if not order_id or not customer_id:
            continue

        # Date normalization (try multiple field names for robustness)
        date_raw = row.get('order_date') or row.get('date') or ''
        date_iso = normalize_date(date_raw)

        # Amount normalization and USD conversion
        raw_amount = row.get('amount', '').strip()
        try:
            amount = float(raw_amount) if raw_amount else 0
        except ValueError:
            amount = 0

        currency = (row.get('currency', '') or '').strip().upper()
        if currency not in CURRENCY_RATE:
            currency = "USD"
        if amount > 0:
            amount = round(amount * CURRENCY_RATE[currency], 2)

        # Now that all the amount values are in USD, updating the currency column to USD
        currency = "USD"
        transformed.append({
            'order_id': order_id,
            'customer_id': customer_id,
            'order_date': date_iso if date_iso else "",
            'amount': amount,
            'currency': currency,
        })
    return transformed

def load_orders(transformed_orders, destination_path):
    """
    Write the transformed orders to a new CSV file. The filename will be the original name
    with '_cleaned.csv' appended before the extension.
    """
    if destination_path.lower().endswith('.csv'):
        base, ext = os.path.splitext(destination_path)
        out_path = base + '_cleaned' + ext
    else:
        out_path = destination_path + '_cleaned.csv'
    fieldnames = ['order_id', 'customer_id', 'order_date', 'amount', 'currency']
    with open(out_path, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in transformed_orders:
            writer.writerow(row)
    print(f"Transformed data written to {out_path}")

    df = pd.DataFrame(transformed_orders)
    df = df[df["order_date"] != ""]
    db.load_dataframe(df)
    print(f"SQLite database written to {db.DB_PATH}")

    semantic_search.rebuild_index(df, Path(out_path), force=True)
    print(f"Semantic index written to {semantic_search.INDEX_DIR}")

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1].lower() == 'load':
        csv_file_path = sys.argv[2]
        raw_orders = read_orders(csv_file_path)
        transformed_orders = transform_orders(raw_orders)
        load_orders(transformed_orders, csv_file_path)


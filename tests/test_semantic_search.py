import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import semantic_search


class SemanticSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        with semantic_search._lock:
            semantic_search._state.index = None
            semantic_search._state.records = ()
            semantic_search._state.model = None

    def test_format_order_text(self) -> None:
        row = pd.Series(
            {
                "customer_id": "C001",
                "amount": 320.0,
                "order_date": date(2024, 3, 15),
            }
        )
        self.assertEqual(
            semantic_search.format_order_text(row),
            "customer C001, $320.00 USD, 2024-03-15",
        )

    @patch("semantic_search._get_model")
    def test_search_returns_top_matches(self, mock_get_model: MagicMock) -> None:
        mock_model = MagicMock()
        mock_get_model.return_value = mock_model

        vectors = np.array(
            [
                [1.0, 0.0],
                [0.9, 0.1],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )
        index = semantic_search.faiss.IndexFlatIP(2)
        index.add(vectors)

        records = (
            semantic_search.OrderRecord("1", "C001", 320.0, "2024-03-15"),
            semantic_search.OrderRecord("2", "C002", 500.0, "2024-03-16"),
            semantic_search.OrderRecord("3", "C003", 120.0, "2024-01-01"),
        )

        with semantic_search._lock:
            semantic_search._state.index = index
            semantic_search._state.records = records

        mock_model.encode.return_value = np.array([[1.0, 0.0]], dtype=np.float32)

        results = semantic_search.search("high value recent orders", top_k=2)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["order_id"], "1")
        self.assertEqual(results[0]["customer_id"], "C001")
        self.assertEqual(results[0]["amount_usd"], 320.0)
        self.assertEqual(results[0]["order_date"], "2024-03-15")
        self.assertAlmostEqual(results[0]["score"], 1.0)
        self.assertEqual(results[1]["order_id"], "2")

    @patch("semantic_search._embed_orders")
    @patch("semantic_search._persist_index")
    def test_rebuild_index_swaps_in_memory_state(
        self,
        mock_persist: MagicMock,
        mock_embed: MagicMock,
    ) -> None:
        index = semantic_search.faiss.IndexFlatIP(2)
        records = (semantic_search.OrderRecord("1", "C001", 10.0, "2024-01-01"),)
        mock_embed.return_value = (index, records)

        df = pd.DataFrame(
            [
                {
                    "order_id": "1",
                    "customer_id": "C001",
                    "order_date": "2024-01-01",
                    "amount": 10.0,
                    "currency": "USD",
                }
            ]
        )

        semantic_search.rebuild_index(df, force=True)

        self.assertTrue(semantic_search.is_ready())
        with semantic_search._lock:
            self.assertIs(semantic_search._state.index, index)
            self.assertEqual(semantic_search._state.records, records)
        mock_persist.assert_not_called()

    @patch("semantic_search.rebuild_index")
    def test_ensure_index_rebuilds_when_stale(
        self,
        mock_rebuild: MagicMock,
    ) -> None:
        csv_path = MagicMock()
        csv_path.exists.return_value = True
        csv_path.stat.return_value.st_mtime = 123.0
        csv_path.resolve.return_value = csv_path

        with patch("semantic_search.is_index_stale", return_value=True):
            df = pd.DataFrame({"order_id": ["1"]})
            semantic_search.ensure_index(df, csv_path)

        mock_rebuild.assert_called_once_with(df, csv_path)


if __name__ == "__main__":
    unittest.main()

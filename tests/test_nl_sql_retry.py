import unittest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

from nl_sql import ORDER_COLUMNS, SqlGenerationError, ask_question


class AskQuestionRetryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        aggregate_sql = (
            "SELECT SUM(amount) AS total_revenue, COUNT(*) AS row_count "
            "FROM orders WHERE customer_id = 'EG-13900'"
        )
        row_sql = (
            f"SELECT {ORDER_COLUMNS} FROM orders "
            "WHERE customer_id = 'EG-13900'"
        )
        cls.good_queries = {
            "aggregate_sql": aggregate_sql,
            "row_sql": row_sql,
        }

    @patch("nl_sql._require_api_key", return_value="test-key")
    @patch("nl_sql.OpenRouter")
    @patch("nl_sql.generate_sql_queries", new_callable=AsyncMock)
    def test_retries_after_failed_aggregate_sql(
        self,
        mock_generate_sql_queries: MagicMock,
        mock_openrouter: MagicMock,
        _mock_require_api_key: MagicMock,
    ) -> None:
        mock_openrouter.return_value.__enter__.return_value = MagicMock()

        # using a missing column to force a runtime error on the first attempt.
        bad_queries = {
            "aggregate_sql": (
                "SELECT SUM(revenue) AS total_revenue, COUNT(*) AS row_count "
                "FROM orders WHERE customer_id = 'EG-13900'"
            ),
            "row_sql": self.good_queries["row_sql"],
        }

        attempts: list[str | None] = []

        async def generate_side_effect(_client, _question, error_context=None):
            attempts.append(error_context)
            if error_context is None:
                return bad_queries, 11
            return self.good_queries, 13

        mock_generate_sql_queries.side_effect = generate_side_effect

        result = asyncio.run(
            ask_question(
                "What is the total revenue from customer EG-13900 in the last 30 days?"
            )
        )

        self.assertEqual(mock_generate_sql_queries.call_count, 2)
        self.assertIsNone(attempts[0])
        self.assertIsNotNone(attempts[1])
        self.assertIn("no such column: revenue", attempts[1].lower())
        self.assertIn("Total revenue", result["answer"])
        self.assertEqual(result["token_count"], 24)
        self.assertGreater(len(result["rows"]), 0)
        self.assertEqual(result["sql_used"], self.good_queries["aggregate_sql"])

    @patch("nl_sql._require_api_key", return_value="test-key")
    @patch("nl_sql.OpenRouter")
    @patch("nl_sql.generate_sql_queries", new_callable=AsyncMock)
    def test_raises_after_second_failed_attempt(
        self,
        mock_generate_sql_queries: MagicMock,
        mock_openrouter: MagicMock,
        _mock_require_api_key: MagicMock,
    ) -> None:
        mock_openrouter.return_value.__enter__.return_value = MagicMock()

        bad_queries = {
            "aggregate_sql": (
                "SELECT SUM(revenue) AS total_revenue, COUNT(*) AS row_count "
                "FROM orders WHERE customer_id = 'EG-13900'"
            ),
            "row_sql": self.good_queries["row_sql"],
        }
        async def return_bad(_client, _question, error_context=None):
            return bad_queries, 5

        mock_generate_sql_queries.side_effect = return_bad

        with self.assertRaises(SqlGenerationError) as ctx:
            asyncio.run(ask_question("What is the total revenue from customer EG-13900?"))

        self.assertEqual(mock_generate_sql_queries.call_count, 2)
        self.assertIn("no such column: revenue", str(ctx.exception).lower())
        self.assertIn("after retry", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()

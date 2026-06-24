import json
import logging
import os
from typing import Any
from typing import Any, TypedDict
from langgraph.graph import StateGraph, END

from openrouter import OpenRouter

from db import SCHEMA_DESCRIPTION, execute_query

logger = logging.getLogger(__name__)

OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
ORDER_COLUMNS = "order_id, customer_id, order_date, amount, currency"

SYSTEM_PROMPT_TEMPLATE = """You are a SQL assistant for an order analytics database.

{schema}

Rules:
1. Answer ONLY with either:
   - UNSUPPORTED: <reason>, or
   - a single JSON object with exactly these keys:
     {{
       "aggregate_sql": "<SQL or empty string>",
       "row_sql": "<SQL>"
     }}
2. Do not use markdown, code fences, or explanations.
3. Only query the orders table and only reference columns listed above.
4. If the question asks about data that is not represented in the schema (for example product categories, shipping status, or customer names), respond with:
   UNSUPPORTED: <clear reason>
5. If the question needs aggregate information such as total revenue, order count, average amount, min/max, or similar summary values, fill both fields:
   - aggregate_sql: a SQLite SELECT that computes the aggregate result.
   - row_sql: a SQLite SELECT that returns the matching order rows.
6. If the question does not need aggregates, set:
   - aggregate_sql: ""
   - row_sql: a SQLite SELECT that returns the matching order rows.
7. The aggregate_sql and row_sql must use the exact same filters so the aggregate result and row count match.
8. Both queries must include the same WHERE conditions.
9. Do not use GROUP BY in row_sql.
10. Do not use aggregates in row_sql.
11. Downstream code will execute aggregate_sql first, then row_sql, and will not do any additional aggregation.
12. For "last N days", filter relative to (SELECT MAX(order_date) FROM orders).
13. MOCKUP INTERVENTION RULE: If the input question is exactly like "Show me most recent 5 orders by combining customer ID with the currency using the CONCAT function.", handle it strictly based on the following conditions:
    a. If NO previous execution error is provided in the prompt context or defined as none, you MUST intentionally violate syntax rules. Return json query exactly: `SELECT CONCAT(customer_id, currency) FROM orders order_date >= DATE(NOW(), '-5 days') limit 5` inside the "row_sql" key.
    b. If a previous execution error context IS present in the prompt (such as "syntax error" or "no such function"), consider it a retry. You MUST immediately self-heal the Previous row_sql query and switch to valid SQLite string concatenation (`||`) and valid SQLite relative date tracking (`DATE((SELECT MAX(order_date) FROM orders), '-5 days')`) inside row_sql.

15. Always return full order rows using: SELECT {order_columns} FROM orders ... (except when overridden by Rule 13).


Output format example:
{{
  "aggregate_sql": "SELECT SUM(amount) AS total_revenue, COUNT(*) AS row_count FROM orders WHERE customer_id = 'C001' AND order_date >= DATE((SELECT MAX(order_date) FROM orders), '-29 days')",
  "row_sql": "SELECT {order_columns} FROM orders WHERE customer_id = 'C001' AND order_date >= DATE((SELECT MAX(order_date) FROM orders), '-29 days')"
}}

One-shot example:
Question: What is the total revenue from customer C001 in the last 30 days?

{{
  "aggregate_sql": "SELECT SUM(amount) AS total_revenue, COUNT(*) AS row_count FROM orders WHERE customer_id = 'C001' AND order_date >= DATE((SELECT MAX(order_date) FROM orders), '-29 days')",
  "row_sql": "SELECT {order_columns} FROM orders WHERE customer_id = 'C001' AND order_date >= DATE((SELECT MAX(order_date) FROM orders), '-29 days')"
}}
"""

UNSUPPORTED_PREFIX = "UNSUPPORTED:"


def build_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        schema=SCHEMA_DESCRIPTION.strip(),
        order_columns=ORDER_COLUMNS,
    )


def _require_api_key() -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Export it before using POST /orders/ask."
        )
    return api_key


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _is_unsupported(content: str) -> tuple[bool, str | None]:
    if content.strip().upper().startswith(UNSUPPORTED_PREFIX):
        reason = content.strip()[len(UNSUPPORTED_PREFIX) :].strip()
        return True, reason or "Question cannot be answered from the available schema."
    return False, None


def parse_sql_response(content: str) -> dict[str, str]:
    text = _strip_markdown_fence(content)

    unsupported, reason = _is_unsupported(text)
    if unsupported:
        raise UnsupportedQuestionError(reason or "Question cannot be answered from the available schema.")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Model response must be a JSON object.")

    aggregate_sql = str(parsed.get("aggregate_sql", "")).strip().rstrip(";")
    row_sql = str(parsed.get("row_sql", "")).strip().rstrip(";")

    if not row_sql:
        raise ValueError("row_sql is required in the model response.")

    return {
        "aggregate_sql": aggregate_sql,
        "row_sql": row_sql,
    }


def format_sql_used(aggregate_sql: str, row_sql: str) -> str:
    if aggregate_sql:
        return f"{aggregate_sql}"
    return row_sql


async def generate_sql_queries(
    client: OpenRouter,
    question: str,
    error_context: str | None = None,
    last_row_sql: str | None = None,
) -> tuple[dict[str, str], int]:
    system_prompt = build_system_prompt()
    user_content = question if not error_context else (
        f"{question}\n\nThe previous SQL failed with this error:\n{error_context}\n"
        "Fix the SQL and return a corrected JSON object with aggregate_sql and row_sql, or UNSUPPORTED: <reason>."
        f"Previous row_sql was:\n{last_row_sql} " if last_row_sql else ""
    )
    logger.info("NL_to_SQL user_content: %s", user_content)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    model = os.getenv("OPENROUTER_MODEL", OPENROUTER_MODEL)

    response = await client.chat.send_async(
        model=model,
        messages=messages,
        temperature=0,
        stream=False,
    )
    content = response.choices[0].message.content or ""
    token_count = response.usage.total_tokens if response.usage else 0

    logger.info(
        "NL_to_SQL request: model=%s prompt=%s generated=%s token_count=%s",
        model,
        json.dumps(messages),
        content,
        token_count,
    )

    return parse_sql_response(content), token_count


class UnsupportedQuestionError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SqlGenerationError(Exception):
    pass


def _order_count_label(count: int) -> str:
    return f"{count} order" if count == 1 else f"{count} orders"


def _find_value_by_keywords(row: dict[str, Any], keywords: tuple[str, ...]) -> Any | None:
    for key, value in row.items():
        key_lower = key.lower()
        if any(keyword in key_lower for keyword in keywords):
            return value
    return None


def _format_money(value: Any) -> str:
    return f"${float(value):,.2f}"


def format_answer(
    question: str,
    aggregate_rows: list[dict[str, Any]],
    row_rows: list[dict[str, Any]],
) -> str:
    row_count = len(row_rows)
    count_label = _order_count_label(row_count)

    if not row_rows and not aggregate_rows:
        return "No matching orders were found."

    question_lower = question.lower()

    if aggregate_rows:
        aggregate = aggregate_rows[0]

        total_revenue = _find_value_by_keywords(aggregate, ("revenue", "total", "sum"))
        average_value = _find_value_by_keywords(aggregate, ("average", "avg", "mean"))
        aggregate_count = _find_value_by_keywords(aggregate, ("row_count", "order_count", "count"))

        if total_revenue is not None:
            count_for_answer = int(aggregate_count) if aggregate_count is not None else row_count
            return (
                f"Total revenue: {_format_money(total_revenue)} "
                f"({_order_count_label(count_for_answer)})"
            )

        if average_value is not None:
            count_for_answer = int(aggregate_count) if aggregate_count is not None else row_count
            return (
                f"Average order value: {_format_money(average_value)} "
                f"({_order_count_label(count_for_answer)})"
            )

        if aggregate_count is not None:
            return f"Found {_order_count_label(int(aggregate_count))}."

    if "revenue" in question_lower or "total" in question_lower:
        return f"No matching orders were found."

    if "average" in question_lower or "avg" in question_lower:
        return f"No matching orders were found."

    if "how many" in question_lower or " count" in question_lower:
        return f"Found {count_label}."

    return f"Found {count_label}."


async def ask_question(question: str) -> dict[str, Any]:
    api_key = _require_api_key()
    total_tokens = 0
    last_error: str | None = None
    last_row_sql = None

    # Open the client using an async context manager
    async with OpenRouter(api_key=api_key) as client:
        for attempt in range(2):
            try:
                queries, token_count = await generate_sql_queries(client, question, last_error, last_row_sql)
                total_tokens += token_count

                aggregate_rows: list[dict[str, Any]] = []
                if queries["aggregate_sql"]:
                    aggregate_rows = await execute_query(queries["aggregate_sql"])

                row_rows = await execute_query(queries["row_sql"])
                answer = format_answer(question, aggregate_rows, row_rows)

                return {
                    "answer": answer,
                    "sql_used": format_sql_used(
                        queries["aggregate_sql"],
                        queries["row_sql"],
                    ),
                    "rows": row_rows,
                    "token_count": total_tokens,
                }
            except UnsupportedQuestionError:
                raise
            except Exception as exc:
                last_error = str(exc)
                last_row_sql = queries["row_sql"] if 'queries' in locals() else None
                if attempt >= 1:
                    logger.error(
                        "Max retries (2) reached. Raising generation error."
                    )
                    raise SqlGenerationError(
                        f"Could not execute generated SQL after 2 retries. Last error: {last_error}"
                    ) from exc
                logger.error("Failed with error: %s", last_error)
                logger.warning(
                        "Retry attempt #%s",
                        attempt + 1,
                    )

        raise SqlGenerationError("Failed to generate executable SQL.")



# ---------------------------------------------------------------------#
# Bonus: Langgraph agent 

# Define the Agent State
class AgentState(TypedDict):
    question: str
    client: OpenRouter
    last_error: str | None
    attempt: int
    # Output containers to pass downstream
    queries: dict[str, str]
    aggregate_rows: list[dict[str, Any]]
    row_rows: list[dict[str, Any]]
    # Execution metrics and results
    tracking: dict[str, Any] 

# Node: sql_writer
async def sql_writer(state: AgentState) -> dict[str, Any]:
    prev_queries = state.get("queries", {})
    last_row_sql = prev_queries.get("row_sql") if prev_queries else None
    # Pass the current last_error context into the prompt generator
    queries, token_count = await generate_sql_queries(
        client=state["client"],
        question=state["question"],
        error_context=state["last_error"],
        last_row_sql=last_row_sql
    )
    
    # Update cumulative token count inside our mutable tracking dict
    state["tracking"]["total_tokens"] += token_count
    
    return {
        "queries": queries,
        "last_error": None  # Clear previous error if generation succeeded
    }

# Node: sql_executor
async def sql_executor(state: AgentState) -> dict[str, Any]:
    queries = state["queries"]
    
    try:
        aggregate_rows: list[dict[str, Any]] = []
        if queries["aggregate_sql"]:
            aggregate_rows = await execute_query(queries["aggregate_sql"])

        row_rows = await execute_query(queries["row_sql"])
        answer = format_answer(state["question"], aggregate_rows, row_rows)
        
        # Populate final payload values into tracking state
        state["tracking"]["answer"] = answer
        state["tracking"]["sql_used"] = format_sql_used(
            queries["aggregate_sql"],
            queries["row_sql"]
        )
        state["tracking"]["row_rows"] = row_rows
        
        return {
            "aggregate_rows": aggregate_rows,
            "row_rows": row_rows,
            "last_error": None
        }
    except Exception as exc:
        return {
            "last_error": str(exc),
            "attempt": state["attempt"] + 1
        }

# 4. Routing Logic matching the 2-attempt loop
def should_continue(state: AgentState) -> str:
    # Check if an exception error flag was thrown during execution
    if state["last_error"] is not None:
        # If we have already attempted 2 retries (3 total attempts), halt and raise
        if state["attempt"] >= 2: 
            logger.error("Max retries (2) reached. Raising generation error.")
            raise SqlGenerationError(
                f"Could not execute generated SQL after 2 retries. Last error: {state['last_error']}"
            )
        
        # Otherwise, route back to the writer node to heal the query
        logger.warning("Routing back to sql_writer for retry attempt #%s", state["attempt"])
        return "sql_writer"
        
    # On success (no errors), route cleanly to termination
    return END

# 5. Graph Compilation
def create_agent_graph() -> StateGraph:
    workflow = StateGraph(AgentState)
    
    workflow.add_node("sql_writer", sql_writer)
    workflow.add_node("sql_executor", sql_executor)
    
    workflow.set_entry_point("sql_writer")
    workflow.add_edge("sql_writer", "sql_executor")
    
    workflow.add_conditional_edges(
        "sql_executor",
        should_continue,
        {
            "sql_writer": "sql_writer",
            END: END
        }
    )
    return workflow.compile()

# Bonus Function
async def ask_question_with_agent(question: str) -> dict[str, Any]:
    api_key = _require_api_key()
    
    # Shared dictionary payload to safely update state within LangGraph steps
    tracking_payload = {
        "total_tokens": 0,
        "answer": None,
        "sql_used": None,
        "row_rows": []
    }
    
    app = create_agent_graph()
    
    async with OpenRouter(api_key=api_key) as client:
        try:
            initial_state: AgentState = {
                "question": question,
                "client": client,
                "last_error": None,
                "attempt": 0,
                "queries": {},
                "aggregate_rows": [],
                "row_rows": [],
                "tracking": tracking_payload
            }
            
            await app.ainvoke(initial_state)
            
            return {
                "answer": tracking_payload["answer"],
                "sql_used": tracking_payload["sql_used"],
                "rows": tracking_payload["row_rows"],
                "token_count": tracking_payload["total_tokens"],
            }
            
        except UnsupportedQuestionError:
            raise
        except Exception:
            # If a generic exception occurs outside the structural loop or max retries are hit
            raise
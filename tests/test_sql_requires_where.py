"""Production Postgres refuses DELETE and UPDATE without WHERE (pg_strict); local ones do not.

Staging and CI run stock Postgres, so a statement that would fail only in production is caught
here instead: every DELETE and UPDATE in the service's SQL and migrations names a WHERE clause.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATEMENT = re.compile(r"\b(DELETE\s+FROM|UPDATE)\b", re.IGNORECASE)
COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def unguarded(sql: str) -> list[str]:
    sql = COMMENT.sub(" ", sql)
    found = []
    for match in STATEMENT.finditer(sql):
        keyword = match.group(1).upper()
        rest = sql[match.end() :]
        if keyword == "UPDATE":
            before = sql[: match.start()].rstrip().upper()
            # FOR UPDATE, FOR NO KEY UPDATE, ON CONFLICT ... DO UPDATE: not UPDATE statements.
            if before.endswith(("FOR", "KEY", "DO")):
                continue
            if not re.match(r'\s+[\w."]+(\s+(AS\s+)?\w+)?\s+SET\b', rest, re.IGNORECASE):
                continue
        end = rest.find(";")
        body = rest if end < 0 else rest[:end]
        depth = 0
        for index, char in enumerate(body):
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:  # the end of an enclosing CTE
                    body = body[:index]
                    break
                depth -= 1
        if not re.search(r"\bWHERE\b", body, re.IGNORECASE):
            found.append(" ".join(sql[match.start() : match.start() + 100].split()))
    return found


def service_sql() -> list[tuple[str, str]]:
    sources = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and STATEMENT.search(node.value)
                and re.search(r"\b(SET|FROM)\b", node.value)
            ):
                sources.append((f"{path.relative_to(ROOT)}:{node.lineno}", node.value))
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        sources.append((str(path.relative_to(ROOT)), path.read_text()))
    return sources


def test_every_delete_and_update_has_a_where_clause():
    offenders = [
        f"{where}: {statement}" for where, sql in service_sql() for statement in unguarded(sql)
    ]
    assert not offenders, (
        "add a WHERE clause (WHERE true for a deliberate full-table change):\n"
        + ("\n".join(offenders))
    )


def test_the_check_recognizes_unguarded_statements():
    assert unguarded("DELETE FROM runs")
    assert unguarded("UPDATE runs SET status = 'x'; SELECT 1 WHERE true")
    assert unguarded("WITH gone AS (DELETE FROM runs RETURNING id) SELECT * FROM gone WHERE true")
    assert not unguarded("DELETE FROM runs WHERE id = $1")
    assert not unguarded("UPDATE runs r SET status = 'x' -- no; WHERE yet\nWHERE r.id = $1")
    assert not unguarded("SELECT id FROM runs WHERE id = $1 FOR UPDATE")
    assert not unguarded("INSERT INTO t VALUES (1) ON CONFLICT (id) DO UPDATE SET v = 1")

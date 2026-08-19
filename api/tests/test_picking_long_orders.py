"""GET /api/admin/sales-orders include_line_count opt-in.

The Picking Tickets "Long Orders" filter needs each row's line-item count.
It is derived live from sales_order_lines on read (not a stored column) so
it always reflects the SO's CURRENT lines, and it is opt-in so pages that
do not filter by size skip the per-row COUNT.
"""

from db_test_context import get_raw_connection


def _list(client, auth_headers, query):
    resp = client.get(f"/api/admin/sales-orders?{query}", headers=auth_headers)
    assert resp.status_code == 200
    return resp.get_json()["sales_orders"]


def _line_counts():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT so_id, COUNT(*) FROM sales_order_lines GROUP BY so_id")
    counts = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return counts


class TestIncludeLineCount:
    def test_line_count_absent_without_the_flag(self, client, auth_headers):
        rows = _list(client, auth_headers, "per_page=1000")
        assert rows, "expected seeded sales orders"
        assert all("line_count" not in r for r in rows)

    def test_line_count_matches_actual_line_rows(self, client, auth_headers):
        expected = _line_counts()
        rows = _list(client, auth_headers, "include_line_count=true&per_page=1000")
        assert rows
        for r in rows:
            assert "line_count" in r
            # SOs with no lines are absent from the GROUP BY -> count 0.
            assert r["line_count"] == expected.get(r["so_id"], 0)

    def test_line_count_reflects_a_newly_added_line(self, client, auth_headers):
        # Prove it is derived, not frozen: adding a line to an SO bumps the
        # count the very next read, with no creation-time flag to go stale.
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("SELECT so_id FROM sales_order_lines LIMIT 1")
        so_id = cur.fetchone()[0]
        cur.execute("SELECT item_id FROM items LIMIT 1")
        item_id = cur.fetchone()[0]

        def count_for(target):
            rows = _list(client, auth_headers, "include_line_count=true&per_page=1000")
            return next(r["line_count"] for r in rows if r["so_id"] == target)

        before = count_for(so_id)
        cur.execute("SELECT COALESCE(MAX(line_number), 0) + 1 FROM sales_order_lines WHERE so_id = %s", (so_id,))
        next_line = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sales_order_lines (so_id, line_number, item_id, quantity_ordered) "
            "VALUES (%s, %s, %s, 1)",
            (so_id, next_line, item_id),
        )
        try:
            assert count_for(so_id) == before + 1
        finally:
            cur.execute(
                "DELETE FROM sales_order_lines WHERE so_id = %s AND line_number = %s",
                (so_id, next_line),
            )
            cur.close()

"""A preferred-bin list is a strict priority hierarchy.

Two bins must never share a priority for the same item (the duplicate
priority-1 rows are what made putaway ambiguous). The admin write
paths auto-resequence to a contiguous 1..K, with the just-changed bin
winning its requested priority and the rest cascading down. Backed by
the deferrable UNIQUE(item_id, priority) constraint (mig 069).

Bins used: bin_id 3 = A-01-01 (Pickable), bin_id 4 = A-01-02
(Pickable); item_id 1.
"""

from db_test_context import get_raw_connection


def _rows_for_item(item_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT bin_id, priority FROM preferred_bins WHERE item_id = %s "
        "ORDER BY priority ASC",
        (item_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return [(r[0], r[1]) for r in rows]


def _pbid(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT preferred_bin_id FROM preferred_bins WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _post(client, auth_headers, bin_id, priority):
    return client.post(
        "/api/admin/preferred-bins",
        json={"item_id": 1, "bin_id": bin_id, "priority": priority},
        headers=auth_headers,
    )


class TestAdminPriorityResequence:
    def test_second_bin_at_same_priority_demotes_the_first(self, client, auth_headers):
        assert _post(client, auth_headers, 3, 1).status_code in (200, 201)
        # bin 4 also requests priority 1 -> it wins; bin 3 cascades to 2.
        assert _post(client, auth_headers, 4, 1).status_code in (200, 201)
        assert _rows_for_item(1) == [(4, 1), (3, 2)]

    def test_no_two_bins_ever_share_a_priority(self, client, auth_headers):
        _post(client, auth_headers, 3, 1)
        _post(client, auth_headers, 4, 1)
        priorities = [p for _b, p in _rows_for_item(1)]
        assert len(priorities) == len(set(priorities)), "priorities must be unique"
        assert priorities == [1, 2], "and contiguous 1..K"

    def test_put_repriority_resequences(self, client, auth_headers):
        _post(client, auth_headers, 3, 1)  # bin3 -> 1
        _post(client, auth_headers, 4, 2)  # bin4 -> 2
        # Promote bin 4 to priority 1 via the inline edit; bin 3 demotes.
        resp = client.put(
            f"/api/admin/preferred-bins/{_pbid(1, 4)}",
            json={"priority": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert _rows_for_item(1) == [(4, 1), (3, 2)]

    def test_put_high_number_collapses_to_contiguous(self, client, auth_headers):
        _post(client, auth_headers, 3, 1)
        _post(client, auth_headers, 4, 2)
        # Send bin 3 to priority 99 -> it becomes the lowest, renumbered 2.
        resp = client.put(
            f"/api/admin/preferred-bins/{_pbid(1, 3)}",
            json={"priority": 99},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert _rows_for_item(1) == [(4, 1), (3, 2)]

    def test_put_missing_returns_404(self, client, auth_headers):
        resp = client.put(
            "/api/admin/preferred-bins/999999",
            json={"priority": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 404

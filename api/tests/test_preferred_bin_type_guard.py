"""Preferred bins must be Pickable.

A Staging / PickableStaging bin holds transient receiving + putaway
stock. Promoting one to an item's preferred bin (the set_as_primary
path fires when stock is received into a staging bin) makes the SKU
render twice at the same priority in putaway and blocks receiving.
Both write paths -- putaway update-preferred and admin POST
/preferred-bins -- must reject any bin whose bin_type is not Pickable.

Seed bins used: bin_id 1 = RECV-01 (Staging), bin_id 3 = A-01-01
(Pickable), bin_id 4 = A-01-02 (Pickable), bin_id 5 = A-01-03
(Pickable, retyped to PickableStaging in the dual-bin cases).
"""

from db_test_context import get_raw_connection


def _query_one(sql, params=None):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    return row


def _retype_bin(bin_id, bin_type):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("UPDATE bins SET bin_type = %s WHERE bin_id = %s", (bin_type, bin_id))
    cur.close()


class TestPutawayUpdatePreferredGuard:
    def test_rejects_staging_bin(self, client, auth_headers):
        resp = client.post(
            "/api/putaway/update-preferred",
            json={"item_id": 1, "bin_id": 1},  # RECV-01, Staging
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Pickable" in resp.get_json()["error"]
        # No preferred row, and the item's default_bin must not have moved.
        assert _query_one(
            "SELECT 1 FROM preferred_bins WHERE item_id = 1 AND bin_id = 1"
        ) is None
        assert _query_one("SELECT default_bin_id FROM items WHERE item_id = 1")[0] != 1

    def test_rejects_pickable_staging_bin(self, client, auth_headers):
        _retype_bin(5, "PickableStaging")
        resp = client.post(
            "/api/putaway/update-preferred",
            json={"item_id": 1, "bin_id": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "PickableStaging" in resp.get_json()["error"]
        assert _query_one(
            "SELECT 1 FROM preferred_bins WHERE item_id = 1 AND bin_id = 5"
        ) is None

    def test_accepts_pickable_bin(self, client, auth_headers):
        resp = client.post(
            "/api/putaway/update-preferred",
            json={"item_id": 1, "bin_id": 4},  # A-01-02, Pickable
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert _query_one(
            "SELECT 1 FROM preferred_bins WHERE item_id = 1 AND bin_id = 4"
        ) is not None


class TestAdminCreatePreferredBinGuard:
    def test_rejects_staging_bin(self, client, auth_headers):
        resp = client.post(
            "/api/admin/preferred-bins",
            json={"item_id": 1, "bin_id": 1, "priority": 1},  # Staging
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Pickable" in resp.get_json()["error"]
        assert _query_one(
            "SELECT 1 FROM preferred_bins WHERE item_id = 1 AND bin_id = 1"
        ) is None

    def test_rejects_pickable_staging_bin(self, client, auth_headers):
        _retype_bin(5, "PickableStaging")
        resp = client.post(
            "/api/admin/preferred-bins",
            json={"item_id": 1, "bin_id": 5, "priority": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert _query_one(
            "SELECT 1 FROM preferred_bins WHERE item_id = 1 AND bin_id = 5"
        ) is None

    def test_accepts_pickable_bin(self, client, auth_headers):
        resp = client.post(
            "/api/admin/preferred-bins",
            json={"item_id": 1, "bin_id": 3, "priority": 1},  # A-01-01, Pickable
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201)
        assert _query_one(
            "SELECT 1 FROM preferred_bins WHERE item_id = 1 AND bin_id = 3"
        ) is not None

    def test_missing_bin_is_404(self, client, auth_headers):
        resp = client.post(
            "/api/admin/preferred-bins",
            json={"item_id": 1, "bin_id": 999999, "priority": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 404

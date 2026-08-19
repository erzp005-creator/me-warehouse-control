import { useState, useEffect, useMemo } from 'react';
import { api } from '../api.js';
import { formatDateOnly } from '../utils/date.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import { PRINT_BATCH_LIMIT } from './pickingConstants.js';
import { groupOrdersByAddress } from './pickingGroups.js';

// Statuses that still have something useful to put on a printed
// picking ticket. SHIPPED/CANCELLED orders are skipped from the
// switcher so we don't accidentally hand a picker a slip for a done
// order. OPEN is the default because that's what the warehouse pulls
// from first thing in the morning.
const PICKABLE_STATUSES = ['OPEN', 'PICKED'];
const STATUS_OPTIONS = [...PICKABLE_STATUSES, 'ALL'];

// Open an individual picking ticket in a standalone tab (no admin
// Layout chrome) so the ticket fills the window and the per-ticket
// print CSS does not fight a sidebar/topbar parent. Mirrors the
// Print All flow.
function openTicketInNewTab(soId) {
  window.open(`/picking-tickets/${soId}/print`, '_blank', 'noopener');
}

export default function PickingTickets() {
  const { warehouseId } = useWarehouse();
  const [status, setStatus] = useState('OPEN');
  const [orders, setOrders] = useState([]);
  const [search, setSearch] = useState('');
  const [lookupError, setLookupError] = useState('');
  // Bump this to force a refetch of the list under the current filters.
  // Lets a manual Refresh button pull fresh sales-order data without
  // requiring the user to navigate away and back (e.g. after an
  // upstream connector updates customer details on already-pushed
  // SOs).
  const [refreshCounter, setRefreshCounter] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  // Default sort: ship-by date ascending so the oldest must-ship-by
  // orders rise to the top of the queue. Picker can re-sort by
  // clicking any column header.
  const [sortKey, setSortKey] = useState('ship_by_date');
  const [sortDir, setSortDir] = useState('asc');
  // mig 064: default to hiding orders whose picking ticket was
  // already confirm-rendered. Operator opts back in to verify a
  // reprint or audit historical queue state.
  const [hidePrinted, setHidePrinted] = useState(true);
  // Multi-Orders: when on, collapse the queue to only those orders that
  // share a shipping address with another order in the queue, clustered
  // by address, so the operator can print and box same-destination orders
  // as one combined shipment and save on postage. Off by default -- the
  // normal one-ticket-per-order view is what the warehouse pulls from.
  const [multiOrders, setMultiOrders] = useState(false);

  useEffect(() => {
    if (!warehouseId) return;
    let cancelled = false;
    setRefreshing(true);
    (async () => {
      const statuses = status === 'ALL' ? PICKABLE_STATUSES : [status];
      const responses = await Promise.all(
        statuses.map((s) => {
          const qs = new URLSearchParams({
            status: s,
            warehouse_id: String(warehouseId),
            // Fetch up to a full print batch so the operator can see -
            // and hand off to Print All - the whole printable queue.
            per_page: String(PRINT_BATCH_LIMIT),
            // Picking-tickets queue wants the lowest-pick_sequence
            // bin per SO so the picker can walk the warehouse in
            // physical order. The flag opts into the per-row
            // primary-bin subquery.
            include_primary_bin: 'true',
          });
          if (hidePrinted) qs.set('hide_printed', 'true');
          return api.get(`/admin/sales-orders?${qs}`);
        }),
      );
      const all = [];
      for (const res of responses) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.sales_orders || []));
        }
      }
      if (!cancelled) {
        setOrders(all);
        setRefreshing(false);
      }
    })();
    return () => { cancelled = true; };
  }, [warehouseId, status, refreshCounter, hidePrinted]);

  async function openTicket() {
    const term = search.trim();
    if (!term) return;
    setLookupError('');
    // Reuse the existing list endpoint so we go through the same
    // auth/role gate as everything else; the detail endpoint takes an
    // so_id (int), not so_number, so we resolve the number first.
    const res = await api.get(
      `/admin/sales-orders?q=${encodeURIComponent(term)}&per_page=10`,
    );
    if (!res?.ok) {
      setLookupError('Could not search sales orders.');
      return;
    }
    const data = await res.json();
    const matches = data.sales_orders || [];
    const exact = matches.find((o) => o.so_number === term) || matches[0];
    if (!exact) {
      setLookupError(`No sales order found matching "${term}".`);
      return;
    }
    openTicketInNewTab(exact.so_id);
  }

  function onSearchKey(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      openTicket();
    }
  }

  function printAll() {
    const qs = new URLSearchParams({ status });
    if (warehouseId) qs.set('warehouse_id', String(warehouseId));
    // Hand the print tab the exact SOs the operator is looking at, in
    // the exact on-screen order. so_ids is the single source of truth
    // for both the set (Hide Printed + any Multi-Orders filter already
    // applied) and the order (current column sort, or the address
    // clustering in Multi-Orders view); the print tab renders precisely
    // these and never re-derives the queue. Capped at one batch per tab -
    // if the queue is larger, the operator prints the top batch, those
    // SOs drop off via Hide Printed, then Print All again for the next.
    const orderedIds = displayOrders.slice(0, PRINT_BATCH_LIMIT).map((o) => o.so_id);
    if (orderedIds.length > 0) {
      qs.set('so_ids', orderedIds.join(','));
    }
    // Open in a new tab so the print queue renders standalone (no
    // admin Layout chrome) and the user can leave it open while
    // continuing to work in the original tab.
    window.open(`/picking-tickets/print-all?${qs.toString()}`, '_blank', 'noopener');
  }

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true, sortable: true },
    { key: 'customer_name', label: 'Customer', sortable: true },
    {
      key: 'ship_by_date',
      label: 'Ship By',
      mono: true,
      sortable: true,
      render: (r) => (r.ship_by_date ? formatDateOnly(r.ship_by_date) : '-'),
    },
    { key: 'ship_method', label: 'Ship Method', sortable: true, render: (r) => r.ship_method || '-' },
    {
      // Lowest-pick_sequence preferred bin across the SO's line items.
      // Sorting by this column orders the queue in warehouse-walking
      // order so the picker can fill the cart left-to-right and the
      // shipper unpacks in the same order.
      key: 'primary_bin_code',
      label: 'Bin',
      mono: true,
      sortable: true,
      render: (r) => r.primary_bin_code || '-',
    },
    { key: 'status', label: 'Status', sortable: true, render: (r) => <StatusTag status={r.status} /> },
    {
      key: 'actions',
      label: '',
      render: (r) => (
        <button
          className="btn btn-sm btn-primary"
          onClick={(e) => {
            e.stopPropagation();
            openTicketInNewTab(r.so_id);
          }}
        >Print Ticket</button>
      ),
    },
  ];

  // In Multi-Orders view, lead with a Group column so the operator can
  // see at a glance which rows box together (#1, #2, ...) and how many
  // orders are in each cluster. Not sortable -- the clustering is the
  // order, and manual sort is suspended in this view.
  if (multiOrders) {
    columns.unshift({
      key: '_group',
      label: 'Group',
      mono: true,
      render: (r) => `#${r._groupIndex} (${r._groupSize})`,
    });
  }

  function handleSort(key) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  // Sort happens client-side because the list is already paged to 50
  // per status (typically the entire queue) and the picker wants the
  // visible rows reordered immediately without a round trip.
  const sortedOrders = useMemo(() => {
    if (!sortKey) return orders;
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...orders].sort((a, b) => {
      // Sorting by the Bin column uses pick_sequence (numeric) so the
      // queue order matches physical walking order in the warehouse,
      // not bin-code alphabetical (which would mix aisles randomly).
      const key = sortKey === 'primary_bin_code' ? 'primary_bin_pick_sequence' : sortKey;
      let av = a[key];
      let bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;  // nulls always at the bottom
      if (bv == null) return -1;
      if (key === 'ship_by_date') {
        return (new Date(av) - new Date(bv)) * dir;
      }
      if (key === 'primary_bin_pick_sequence') {
        return (av - bv) * dir;
      }
      return String(av).localeCompare(String(bv), undefined, { numeric: true }) * dir;
    });
  }, [orders, sortKey, sortDir]);

  // Multi-Orders view: address-groups (2+ orders sharing a shipping
  // address), most-urgent group first, members kept adjacent. Derived
  // from the raw queue -- not sortedOrders -- so the clustering holds
  // regardless of any column sort the operator last clicked.
  const addressGroups = useMemo(() => groupOrdersByAddress(orders), [orders]);

  // The rows the table + Print All actually use. In the default view this
  // is just the sorted queue. In Multi-Orders view it is the grouped
  // orders flattened into one clustered list, each row tagged with its
  // 1-based group number and the group's size so the Group column can
  // label the cluster.
  const displayOrders = useMemo(() => {
    if (!multiOrders) return sortedOrders;
    const out = [];
    addressGroups.forEach((group, gi) => {
      group.orders.forEach((o) => {
        out.push({ ...o, _groupIndex: gi + 1, _groupSize: group.orders.length });
      });
    });
    return out;
  }, [multiOrders, sortedOrders, addressGroups]);

  return (
    <div>
      <PageHeader title="Picking Tickets" />

      <div className="section">
        <div className="section-title">Find a ticket</div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', maxWidth: 520 }}>
          <input
            className="form-input"
            style={{ flex: 1 }}
            placeholder="Sales order number, e.g. 648415"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={onSearchKey}
          />
          <button className="btn btn-primary" onClick={openTicket}>Open</button>
        </div>
        {lookupError && (
          <div className="form-error" style={{ marginTop: 8 }}>{lookupError}</div>
        )}
      </div>

      <div className="section">
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 12,
            marginBottom: 8,
          }}
        >
          <div className="section-title" style={{ marginBottom: 0 }}>Orders ready to pick</div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <label style={{ fontSize: 13, color: '#555' }}>Status</label>
            <select
              className="form-input"
              style={{ width: 'auto' }}
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <label style={{
              display: 'flex', alignItems: 'center', gap: 6,
              fontSize: 13, color: '#555', cursor: 'pointer',
            }} title="Hide orders whose picking ticket has already been rendered (mig 064 printed_at)">
              <input
                type="checkbox"
                checked={hidePrinted}
                onChange={(e) => setHidePrinted(e.target.checked)}
              />
              Hide Printed
            </label>
            <label style={{
              display: 'flex', alignItems: 'center', gap: 6,
              fontSize: 13, color: '#555', cursor: 'pointer',
            }} title="Show only orders that share a shipping address with another order in the queue, clustered so they can be boxed and shipped together">
              <input
                type="checkbox"
                checked={multiOrders}
                onChange={(e) => setMultiOrders(e.target.checked)}
              />
              Multi-Orders
            </label>
            <button
              className="btn btn-secondary"
              onClick={() => setRefreshCounter((c) => c + 1)}
              disabled={refreshing}
              title="Re-fetch the list from the server (e.g. to pick up just-pushed customer name + shipping address)"
            >
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </button>
            <button
              className="btn btn-primary"
              onClick={printAll}
              disabled={displayOrders.length === 0}
              title={
                displayOrders.length > PRINT_BATCH_LIMIT
                  ? `Queue has ${displayOrders.length}; one tab prints the top ${PRINT_BATCH_LIMIT} in the current sort. Clear them and Print All again for the rest.`
                  : undefined
              }
            >
              {displayOrders.length > PRINT_BATCH_LIMIT
                ? `Print First ${PRINT_BATCH_LIMIT} (of ${displayOrders.length})`
                : `Print All (${displayOrders.length})`}
            </button>
          </div>
        </div>
        <DataTable
          columns={columns}
          data={displayOrders}
          emptyMessage={
            multiOrders
              ? 'No multi-order groups in this queue'
              : 'No orders ready for picking'
          }
          onRowClick={(r) => openTicketInNewTab(r.so_id)}
          // In Multi-Orders view the address clustering IS the order, so
          // manual column sort is suspended (no onSort -> headers inert).
          sortKey={multiOrders ? null : sortKey}
          sortDir={multiOrders ? null : sortDir}
          onSort={multiOrders ? undefined : handleSort}
        />
      </div>
    </div>
  );
}

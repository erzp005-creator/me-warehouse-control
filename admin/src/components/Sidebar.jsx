import { NavLink, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useAuth } from '../auth.jsx';
import { useWarehouse } from '../warehouse.jsx';

// Each NAV item carries a page_key matching api/constants.py
// ALL_PAGE_KEYS. Sidebar filters by the user's allowed_pages so a
// USER without the grant never sees the link (and the backend
// rejects direct URL hits via @require_admin_or_page_permission).
// Dashboard intentionally has no page_key: it is reachable by any
// authenticated user so the badges below populate.
const NAV = [
  {
    label: 'Floor',
    items: [
      { to: '/', label: 'Dashboard', pageKey: 'dashboard' },
      { to: '/inventory', label: 'Inventory', pageKey: 'inventory' },
      { to: '/cycle-counts', label: 'Counts', pageKey: 'cycle-counts' },
      { to: '/count-approvals', label: 'Approvals', pageKey: 'count-approvals' },
    ],
  },
  {
    label: 'Inbound',
    items: [
      { to: '/purchase-orders', label: 'Purchase Orders', pageKey: 'purchase-orders' },
      { to: '/receiving', label: 'Receiving', pageKey: 'receiving' },
      { to: '/putaway', label: 'Put-away', pageKey: 'putaway' },
    ],
  },
  {
    label: 'Outbound',
    items: [
      { to: '/sales-orders', label: 'Sales Orders', pageKey: 'sales-orders' },
      { to: '/pos-activity', label: 'POS Activity' },
      { to: '/picking-tickets', label: 'Picking Tickets', pageKey: 'picking-tickets' },
      // Picking/Packing/Shipping are mobile-only: the admin-side
      // mirrors were retired. Supervisors use Sales Orders + Picking
      // Tickets here, and the handheld scanners drive the floor work.
      { to: '/picking-batches', label: 'Picking Batches', pageKey: 'picking-batches' },
    ],
  },
  {
    label: 'Warehouse',
    items: [
      // Day-to-day operational entries lead; the warehouse-layout
      // pages (Warehouses / Bins / Zones / Preferred Bins) collapse
      // into a single Data link with a tab strip.
      { to: '/items', label: 'Items', pageKey: 'items' },
      { to: '/vendors', label: 'Vendors', pageKey: 'vendors' },
      { to: '/adjustments', label: 'Inventory Adjustments', pageKey: 'adjustments' },
      { to: '/inter-warehouse-transfers', label: 'Inventory Transfers', pageKey: 'inter-warehouse-transfers' },
      { to: '/transfer-orders', label: 'Transfer Orders', pageKey: 'transfer-orders' },
      { to: '/data', label: 'Data', pageKeys: ['warehouses', 'bins', 'zones', 'preferred-bins'] },
    ],
  },
  {
    label: 'System',
    items: [
      { to: '/users', label: 'Users', pageKey: 'users' },
      { to: '/api-tokens', label: 'API tokens', pageKey: 'api-tokens' },
      { to: '/inbound', label: 'Inbound activity', pageKey: 'inbound' },
      { to: '/consumer-groups', label: 'Consumer groups', pageKey: 'consumer-groups' },
      { to: '/webhooks', label: 'Webhooks', pageKey: 'webhooks' },
      { to: '/audit-log', label: 'Audit log', pageKey: 'audit-log' },
      { to: '/imports', label: 'Import', pageKey: 'imports' },
      { to: '/integrations', label: 'Integrations', pageKey: 'integrations' },
      { to: '/settings', label: 'Settings', pageKey: 'settings' },
    ],
  },
];

export default function Sidebar() {
  const location = useLocation();
  const { user } = useAuth();
  const { warehouseId } = useWarehouse();
  const [counts, setCounts] = useState({});

  useEffect(() => {
    if (!warehouseId) return;
    // Same silent treatment for the dashboard counts (sidebar badges).
    // /admin/dashboard is intentionally any-auth so this typically
    // succeeds, but a future tightening should not blow up the UI
    // with a modal on every page load.
    api.get(
      `/admin/dashboard?warehouse_id=${warehouseId}`,
      { silentPermissionDenied: true },
    ).then(async (res) => {
      if (!res || !res.ok) return;
      const data = await res.json();
      setCounts({
        '/receiving': data.open_pos || 0,
        '/putaway': data.pending_putaway || 0,
        '/count-approvals': data.pending_adjustments || 0,
        // /picking, /packing, /shipping badges were removed alongside
        // their retired nav entries; the throughput counts still live
        // on the Dashboard page itself.
        // v1.8.0 (#296): pending TO approvals scoped to the active
        // warehouse (source OR destination match). Falls back to 0
        // when the dashboard endpoint is the older shape.
        '/transfer-orders': data.pending_to_approvals || 0,
      });
    });
  }, [location.pathname, warehouseId]);

  const navGroups = NAV;

  return (
    <nav className="sidebar">
      {navGroups.map((group) => (
        <div key={group.label} className="sidebar-card">
          <div className="sidebar-group-label">{group.label}</div>
          {group.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `sidebar-link${isActive ? ' active' : ''}`
              }
            >
              <span>{item.label}</span>
              {counts[item.to] > 0 && (
                <span className="sidebar-badge">{counts[item.to]}</span>
              )}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}

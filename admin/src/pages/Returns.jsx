import { NavLink, Outlet, Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../auth.jsx';

// Returns consolidates the post-fulfillment goods-in + money-back views under a
// single /returns parent with a tab strip, mirroring /data. The RMA (return SOs)
// and Refunds (credit-memo SOs) sub-pages render as Outlet children and keep
// their own PageHeader + state. Both gate on the sales-orders page key today.
const TABS = [
  { to: 'rma', label: 'RMA', pageKey: 'sales-orders' },
  { to: 'refunds', label: 'Refunds', pageKey: 'sales-orders' },
];

export default function Returns() {
  const { user } = useAuth();
  const location = useLocation();
  const allowedPages = user?.allowed_pages;
  const isAdmin = user?.role === 'ADMIN';

  const visibleTabs = TABS.filter((t) =>
    isAdmin || (Array.isArray(allowedPages) && allowedPages.includes(t.pageKey)),
  );

  if (location.pathname === '/returns' || location.pathname === '/returns/') {
    if (visibleTabs.length === 0) {
      return (
        <div style={{ padding: 24, color: 'var(--text-secondary)' }}>
          No returns pages granted to your account.
        </div>
      );
    }
    return <Navigate to={visibleTabs[0].to} replace />;
  }

  return (
    <div>
      <div className="data-tabs">
        {visibleTabs.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            className={({ isActive }) => `data-tab${isActive ? ' active' : ''}`}
          >
            {tab.label}
          </NavLink>
        ))}
      </div>
      <Outlet />
    </div>
  );
}

/**
 * Long Orders view on the Picking Tickets page.
 *
 * The toggle collapses the queue to orders with MORE THAN 4 line items
 * (5+), the picking-heavy orders, so they can be batch-printed. It is
 * independent of Multi-Orders: with both on, the queue shows long orders
 * that ALSO share a shipping address. line_count comes from the queue
 * endpoint (include_line_count) and the >4 threshold lives in one shared
 * constant.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LONG_ORDER_MIN_LINES } from '../pages/pickingConstants.js';

const apiGetMock = vi.fn();
vi.mock('../api.js', () => ({
  api: {
    get: (...a) => apiGetMock(...a),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));
vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

import PickingTickets from '../pages/PickingTickets.jsx';

const json = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

// Address A is shared by the two 5-line orders (for the compose test);
// every other order is at its own address.
const at = (over) => ({
  status: 'OPEN', order_type: 'sale', warehouse_id: 1,
  shipping_address_line1: '1 A St', shipping_address_city: 'Denver',
  shipping_address_state: 'CO', shipping_address_postal_code: '80202',
  ship_by_date: '2026-07-20', ship_method: 'GROUND',
  ...over,
});
const QUEUE = [
  // Same recipient AND same address: the sequencing key includes the
  // customer name (two different customers at one address never share
  // a shipment), so the combining pair must match on both.
  at({ so_id: 1, so_number: 'SO-2001', customer_name: 'Long A', line_count: 5 }),
  at({ so_id: 2, so_number: 'SO-2002', customer_name: 'Long A', line_count: 5 }),
  // Boundary: exactly 4 lines -> NOT long (the rule is > 4, not >= 4).
  at({ so_id: 3, so_number: 'SO-2003', customer_name: 'Boundary',
       line_count: 4, shipping_address_line1: '3 B St', shipping_address_postal_code: '80203' }),
  at({ so_id: 4, so_number: 'SO-2004', customer_name: 'Short',
       line_count: 1, shipping_address_line1: '4 C St', shipping_address_postal_code: '80204' }),
  // Long singleton at its own address -> shown under Long, dropped when
  // Multi is also on (no same-address sibling among the long orders).
  at({ so_id: 5, so_number: 'SO-2005', customer_name: 'Long Solo',
       line_count: 6, shipping_address_line1: '5 D St', shipping_address_postal_code: '80205' }),
];

beforeEach(() => {
  apiGetMock.mockReset();
  apiGetMock.mockImplementation((path = '') => {
    if (path.includes('/admin/sales-orders')) {
      // The page must ask for line_count so the toggle is instant.
      expect(path).toContain('include_line_count=true');
      return json({ sales_orders: QUEUE, total: QUEUE.length, page: 1, pages: 1, per_page: 200 });
    }
    return json({});
  });
});

function renderPage() {
  return render(
    <MemoryRouter>
      <PickingTickets />
    </MemoryRouter>,
  );
}

describe('PickingTickets Long Orders toggle', () => {
  it('has the threshold set to more-than-4 line items', () => {
    expect(LONG_ORDER_MIN_LINES).toBe(5);
  });

  it('shows the full queue by default (toggle off, no Items column)', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-2001')).toBeInTheDocument());
    ['SO-2002', 'SO-2003', 'SO-2004', 'SO-2005'].forEach((n) =>
      expect(screen.getByText(n)).toBeInTheDocument());
    expect(screen.queryByRole('columnheader', { name: 'Items' })).toBeNull();
  });

  it('keeps only 5+-line orders and shows the Items count when toggled on', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-2001')).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText('Long Orders'));

    // Long: SO-2001 (5), SO-2002 (5), SO-2005 (6) stay.
    await waitFor(() => expect(screen.getByText('SO-2005')).toBeInTheDocument());
    expect(screen.getByText('SO-2001')).toBeInTheDocument();
    expect(screen.getByText('SO-2002')).toBeInTheDocument();
    // The 4-line boundary order and the 1-line order drop out.
    expect(screen.queryByText('SO-2003')).toBeNull();
    expect(screen.queryByText('SO-2004')).toBeNull();
    // Items column appears and shows the counts.
    expect(screen.getByRole('columnheader', { name: 'Items' })).toBeInTheDocument();
    expect(screen.getByText('6')).toBeInTheDocument();
  });

  it('composes with Multi-Orders (AND): only long orders that also share an address', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-2001')).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText('Long Orders'));
    fireEvent.click(screen.getByLabelText('Multi-Orders'));

    // Long AND same-address: only SO-2001 + SO-2002 (both 5 lines, address A).
    await waitFor(() => expect(screen.queryByText('SO-2005')).toBeNull());
    expect(screen.getByText('SO-2001')).toBeInTheDocument();
    expect(screen.getByText('SO-2002')).toBeInTheDocument();
    // Both the Group and Items columns are present in the combined view.
    expect(screen.getByRole('columnheader', { name: 'Group' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Items' })).toBeInTheDocument();
  });

  it('shows a long-specific empty-state when no order is long', async () => {
    apiGetMock.mockImplementation((path = '') => {
      if (path.includes('/admin/sales-orders')) {
        return json({ sales_orders: [QUEUE[3]], total: 1, page: 1, pages: 1, per_page: 200 });
      }
      return json({});
    });
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-2004')).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText('Long Orders'));
    await waitFor(() =>
      expect(screen.getByText('No long orders in this queue')).toBeInTheDocument());
  });
});

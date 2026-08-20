/**
 * SO-modal Related Records tab.
 *
 * The tab shows the viewed order's whole parent/child family -- the
 * original, everything hanging off it, and everything hanging off those.
 * Production families go three deep (a sale, its backorder, and an RMA
 * raised against that backorder), so the tab is a tree, not one hop.
 *
 * Tests pin:
 * - The tab renders with a count, and the count excludes the record
 *   itself so "no relatives" reads as 0.
 * - The empty state when an order stands alone.
 * - Indentation follows depth, so a grandchild hangs off its own parent.
 * - Clicking a related record swaps the modal to it and the header back
 *   arrow returns -- the only route to an RMA from this page, which
 *   filters returns and refunds out of its list.
 * - A voided RMA renders, greyed and tagged, instead of vanishing.
 * - The caret expands line items in place, and a return shows Received
 *   rather than Shipped.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiGetMock = vi.fn();
vi.mock('../api.js', () => ({
  api: {
    get: (...a) => apiGetMock(...a),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));
vi.mock('../auth.jsx', () => ({
  useAuth: () => ({ user: { role: 'ADMIN', allowed_overrides: [] } }),
}));

import SalesOrders from '../pages/SalesOrders.jsx';

const json = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

function so(overrides) {
  return {
    so_id: 1, so_number: 'SO-1', customer_name: 'Cust', status: 'SHIPPED',
    order_type: 'sale', parent_so_id: null,
    order_date: '2026-06-14T00:00:00Z', ship_method: 'GROUND',
    customer_phone: '', customer_email: '', ship_address: '',
    priority: 0, memo: '', ...overrides,
  };
}

function record(overrides) {
  return {
    so_id: 1, so_number: 'SO-1', order_type: 'sale', status: 'SHIPPED',
    warehouse_id: 1, customer_name: 'Cust', parent_so_id: null, depth: 0,
    is_current: false, is_voided: false, voided_at: null,
    cancellation_reason: null, created_at: '2026-06-14T00:00:00Z',
    lines: [], ...overrides,
  };
}

/**
 * Wire the API mock for a family. `records` is the full family; the
 * /related response is rebuilt per requested so_id so is_current tracks
 * whichever record the modal is showing, exactly as the server does.
 */
function wire(orders, records) {
  apiGetMock.mockImplementation((path = '') => {
    const related = path.match(/\/admin\/sales-orders\/(\d+)\/related/);
    if (related) {
      const asked = Number(related[1]);
      return json({
        so_id: asked,
        related_count: records.length - 1,
        records: records.map((r) => ({ ...r, is_current: r.so_id === asked })),
      });
    }
    const detail = path.match(/\/admin\/sales-orders\/(\d+)$/);
    if (detail) {
      const asked = Number(detail[1]);
      const found = orders.find((o) => o.so_id === asked) || orders[0];
      return json({ sales_order: found, lines: [], pick_tasks: [] });
    }
    if (path.includes('/admin/sales-orders')) {
      return json({
        sales_orders: orders, total: orders.length, page: 1, pages: 1,
        per_page: 1000,
      });
    }
    return json({});
  });
}

// Open the VIEW modal by clicking the order's row in the list. (?focus=
// opens the edit modal instead, which has no tabs.)
async function openViewModal(soNumber = 'SO-1') {
  render(
    <MemoryRouter initialEntries={['/sales-orders']}>
      <SalesOrders />
    </MemoryRouter>,
  );
  const cell = await screen.findByText(soNumber);
  await act(async () => { fireEvent.click(cell); });
  await screen.findByText('Order Summary');
}

async function openRelatedTab() {
  await act(async () => {
    fireEvent.click(screen.getByTestId('so-tab-related'));
  });
}

describe('Related Records tab', () => {
  beforeEach(() => { apiGetMock.mockReset(); });

  it('shows a count that excludes the record itself', async () => {
    wire([so()], [
      record({ so_id: 1, so_number: 'SO-1', is_current: true }),
      record({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1, parent_so_id: 1 }),
      record({ so_id: 3, so_number: 'SO-1-BO', order_type: 'backorder', depth: 1, parent_so_id: 1 }),
    ]);
    await openViewModal();
    expect(screen.getByTestId('so-tab-related').textContent).toContain('(2)');
  });

  it('renders the empty state for a standalone order', async () => {
    wire([so()], [record({ so_id: 1, is_current: true })]);
    await openViewModal();
    expect(screen.getByTestId('so-tab-related').textContent).toContain('(0)');
    await openRelatedTab();
    expect(screen.getByTestId('related-empty')).toBeTruthy();
  });

  it('lists every child of the parent with operator-facing type labels', async () => {
    wire([so()], [
      record({ so_id: 1, so_number: 'SO-1', is_current: true }),
      record({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1, parent_so_id: 1 }),
      record({ so_id: 3, so_number: 'SO-1-BO', order_type: 'backorder', depth: 1, parent_so_id: 1 }),
      record({ so_id: 4, so_number: 'SO-1-REFUND', order_type: 'refund', depth: 1, parent_so_id: 1 }),
      record({ so_id: 5, so_number: 'SO-1-EXCHANGE', order_type: 'exchange', depth: 1, parent_so_id: 1 }),
    ]);
    await openViewModal();
    await openRelatedTab();

    for (const id of [2, 3, 4, 5]) {
      expect(screen.getByTestId(`related-row-${id}`)).toBeTruthy();
    }
    // "return" is an RMA to the people using this screen.
    expect(screen.getByText('RMA')).toBeTruthy();
    expect(screen.getByText('Backorder')).toBeTruthy();
    expect(screen.getByText('Refund')).toBeTruthy();
    expect(screen.getByText('Exchange')).toBeTruthy();
  });

  it('marks the current record and makes it unclickable', async () => {
    wire([so()], [
      record({ so_id: 1, so_number: 'SO-1', is_current: true }),
      record({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1, parent_so_id: 1 }),
    ]);
    await openViewModal();
    await openRelatedTab();

    expect(screen.getByTestId('related-row-1').className).toContain('current');
    expect(screen.getByTestId('related-open-1').disabled).toBe(true);
    expect(screen.getByTestId('related-open-2').disabled).toBe(false);
    expect(screen.getByText('YOU ARE HERE')).toBeTruthy();
  });

  it('indents a grandchild deeper than its parent', async () => {
    // The production shape: 652120 -> 652120-BO -> 652120-BO-RMA. The RMA
    // belongs to the backorder, not to the original order, and the tree
    // has to say so.
    wire([so()], [
      record({ so_id: 1, so_number: 'SO-1', depth: 0, is_current: true }),
      record({ so_id: 2, so_number: 'SO-1-BO', order_type: 'backorder', depth: 1, parent_so_id: 1 }),
      record({ so_id: 3, so_number: 'SO-1-BO-RMA', order_type: 'return', depth: 2, parent_so_id: 2 }),
    ]);
    await openViewModal();
    await openRelatedTab();

    const pad = (id) => screen
      .getByTestId(`related-row-${id}`)
      .querySelector('td').style.paddingLeft;
    expect(pad(1)).toBe('8px');
    expect(pad(2)).toBe('30px');
    expect(pad(3)).toBe('52px');
  });

  it('navigates into a related record and back again', async () => {
    wire(
      [so({ so_id: 1, so_number: 'SO-1' }),
       so({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', parent_so_id: 1 })],
      [record({ so_id: 1, so_number: 'SO-1', is_current: true }),
       record({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1, parent_so_id: 1 })],
    );
    await openViewModal();
    await openRelatedTab();

    // No back arrow until the operator has actually navigated somewhere.
    expect(screen.queryByTestId('modal-back')).toBeNull();

    await act(async () => { fireEvent.click(screen.getByTestId('related-open-2')); });
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 2 }).textContent)
        .toBe('SO SO-1-RMA');
    });

    const back = screen.getByTestId('modal-back');
    expect(back.getAttribute('title')).toBe('Back to SO-1');
    await act(async () => { fireEvent.click(back); });
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 2 }).textContent)
        .toBe('SO SO-1');
    });
    // Trail is exhausted, so the arrow goes away again.
    expect(screen.queryByTestId('modal-back')).toBeNull();
  });

  it('keeps the back trail across two hops', async () => {
    wire(
      [so({ so_id: 1, so_number: 'SO-1' }),
       so({ so_id: 2, so_number: 'SO-1-BO', order_type: 'backorder', parent_so_id: 1 }),
       so({ so_id: 3, so_number: 'SO-1-BO-RMA', order_type: 'return', parent_so_id: 2 })],
      [record({ so_id: 1, so_number: 'SO-1', is_current: true }),
       record({ so_id: 2, so_number: 'SO-1-BO', order_type: 'backorder', depth: 1, parent_so_id: 1 }),
       record({ so_id: 3, so_number: 'SO-1-BO-RMA', order_type: 'return', depth: 2, parent_so_id: 2 })],
    );
    await openViewModal();
    await openRelatedTab();

    await act(async () => { fireEvent.click(screen.getByTestId('related-open-2')); });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('SO SO-1-BO'));
    await openRelatedTab();
    await act(async () => { fireEvent.click(screen.getByTestId('related-open-3')); });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('SO SO-1-BO-RMA'));

    await act(async () => { fireEvent.click(screen.getByTestId('modal-back')); });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('SO SO-1-BO'));
    await act(async () => { fireEvent.click(screen.getByTestId('modal-back')); });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('SO SO-1'));
  });

  it('lands on Details when opening a record, and on Related when going back', async () => {
    wire(
      [so({ so_id: 1, so_number: 'SO-1' }),
       so({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', parent_so_id: 1 })],
      [record({ so_id: 1, so_number: 'SO-1', is_current: true }),
       record({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1, parent_so_id: 1 })],
    );
    await openViewModal();
    await openRelatedTab();

    await act(async () => { fireEvent.click(screen.getByTestId('related-open-2')); });
    // Clicking a record shows what is ON it, not the family list again.
    await waitFor(() => expect(screen.getByText('Order Summary')).toBeTruthy());
    expect(screen.getByTestId('so-tab-details').className).toContain('active');

    await act(async () => { fireEvent.click(screen.getByTestId('modal-back')); });
    // Back returns to the list they clicked from.
    await waitFor(() => expect(screen.getByTestId('related-table')).toBeTruthy());
    expect(screen.getByTestId('so-tab-related').className).toContain('active');
  });

  it('shows a voided RMA greyed and tagged rather than hiding it', async () => {
    wire([so()], [
      record({ so_id: 1, so_number: 'SO-1', is_current: true }),
      record({
        so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1,
        parent_so_id: 1, is_voided: true, voided_at: '2026-08-01T00:00:00Z',
      }),
    ]);
    await openViewModal();
    await openRelatedTab();

    const row = screen.getByTestId('related-row-2');
    expect(row).toBeTruthy();
    expect(row.className).toContain('voided');
    expect(screen.getByText('VOIDED')).toBeTruthy();
    // Still reachable: a voided RMA is soft-deleted, not gone.
    expect(screen.getByTestId('related-open-2').disabled).toBe(false);
  });

  it('expands line items in place from the caret', async () => {
    wire([so()], [
      record({ so_id: 1, so_number: 'SO-1', is_current: true }),
      record({
        so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1,
        parent_so_id: 1,
        lines: [{
          so_line_id: 9, line_number: 1, sku: 'SKU-9', item_name: 'Widget',
          quantity_ordered: 2, quantity_shipped: 0, quantity_received: 2,
        }],
      }),
    ]);
    await openViewModal();
    await openRelatedTab();

    expect(screen.queryByTestId('related-lines-2')).toBeNull();
    await act(async () => { fireEvent.click(screen.getByTestId('related-caret-2')); });
    expect(screen.getByTestId('related-lines-2')).toBeTruthy();
    expect(screen.getByText('SKU-9')).toBeTruthy();
    // A return's meaningful quantity is what came back, so the column
    // header swaps from Shipped to Received.
    expect(screen.getByText('Received')).toBeTruthy();

    await act(async () => { fireEvent.click(screen.getByTestId('related-caret-2')); });
    expect(screen.queryByTestId('related-lines-2')).toBeNull();
  });

  it('expanding lines does not navigate away from the record', async () => {
    wire([so()], [
      record({ so_id: 1, so_number: 'SO-1', is_current: true }),
      record({ so_id: 2, so_number: 'SO-1-RMA', order_type: 'return', depth: 1, parent_so_id: 1 }),
    ]);
    await openViewModal();
    await openRelatedTab();

    await act(async () => { fireEvent.click(screen.getByTestId('related-caret-2')); });
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('SO SO-1');
    expect(screen.queryByTestId('modal-back')).toBeNull();
  });

  it('opens the record itself even when the related call fails', async () => {
    apiGetMock.mockImplementation((path = '') => {
      if (path.includes('/related')) return json({ error: 'boom' }, 500);
      if (/\/admin\/sales-orders\/\d+$/.test(path)) {
        return json({ sales_order: so(), lines: [], pick_tasks: [] });
      }
      if (path.includes('/admin/sales-orders')) {
        return json({ sales_orders: [so()], total: 1, page: 1, pages: 1, per_page: 1000 });
      }
      return json({});
    });
    await openViewModal();
    // Details still render, and the tab degrades rather than blocking.
    expect(screen.getByText('Order Summary')).toBeTruthy();
    await openRelatedTab();
    expect(screen.getByText(/could not be loaded/i)).toBeTruthy();
  });
});

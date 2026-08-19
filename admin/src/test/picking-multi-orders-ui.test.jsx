/**
 * Multi-Orders view on the Picking Tickets page + the "SHIP WITH" combine
 * banner on the printed ticket.
 *
 * The toggle collapses the queue to only orders sharing a shipping
 * address (2+), clustered, so the operator can box same-destination
 * orders into one shipment. This exercises the wiring end to end: the
 * filter hides singletons, the Group column labels each cluster, and the
 * TicketDocument stamps the combine banner only when it has siblings.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

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
import { TicketDocument } from '../pages/PickingTicketPrint.jsx';

const json = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

// so_id 1 & 2 ship to the same Denver address; so_id 3 is a lone order to
// a different address -- so exactly one 2-order group exists.
const at = (over) => ({
  status: 'OPEN', order_type: 'sale', warehouse_id: 1,
  shipping_address_line1: '12 Elm St', shipping_address_line2: '',
  shipping_address_city: 'Denver', shipping_address_state: 'CO',
  shipping_address_postal_code: '80202',
  ship_by_date: '2026-07-20', ship_method: 'GROUND',
  ...over,
});
const QUEUE = [
  at({ so_id: 1, so_number: 'SO-1001', customer_name: 'Ada Byron' }),
  at({ so_id: 2, so_number: 'SO-1002', customer_name: 'Ada Byron' }),
  at({
    so_id: 3, so_number: 'SO-1003', customer_name: 'Grace Hopper',
    shipping_address_line1: '99 Oak Ave', shipping_address_postal_code: '80301',
  }),
];

beforeEach(() => {
  apiGetMock.mockReset();
  apiGetMock.mockImplementation((path = '') => {
    if (path.includes('/admin/sales-orders')) {
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

describe('PickingTickets Multi-Orders toggle', () => {
  it('shows the full queue by default (toggle off)', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-1001')).toBeInTheDocument());
    expect(screen.getByText('SO-1002')).toBeInTheDocument();
    expect(screen.getByText('SO-1003')).toBeInTheDocument();
    // No Group column in the default view.
    expect(screen.queryByRole('columnheader', { name: 'Group' })).toBeNull();
  });

  it('filters to only same-address groups and labels the cluster when toggled on', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-1001')).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText('Multi-Orders'));

    // The 2-order Denver group stays; the lone SO-1003 drops out.
    await waitFor(() => expect(screen.queryByText('SO-1003')).toBeNull());
    expect(screen.getByText('SO-1001')).toBeInTheDocument();
    expect(screen.getByText('SO-1002')).toBeInTheDocument();

    // Group column appears and labels the cluster #1 with its size (2).
    expect(screen.getByRole('columnheader', { name: 'Group' })).toBeInTheDocument();
    expect(screen.getAllByText('#1 (2)')).toHaveLength(2);
  });

  it('does NOT group two different customers at the same address', async () => {
    apiGetMock.mockImplementation((path = '') => {
      if (path.includes('/admin/sales-orders')) {
        return json({
          sales_orders: [
            at({ so_id: 1, so_number: 'SO-1001', customer_name: 'Ada Byron' }),
            at({ so_id: 2, so_number: 'SO-1002', customer_name: 'Grace Hopper' }),
          ],
          total: 2, page: 1, pages: 1, per_page: 200,
        });
      }
      return json({});
    });
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-1001')).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText('Multi-Orders'));
    // Same street address, different recipients: never a combine group.
    await waitFor(() =>
      expect(screen.getByText('No multi-order groups in this queue')).toBeInTheDocument(),
    );
  });

  it('passes combine=1 to the print tab only from the Multi-Orders view', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-1001')).toBeInTheDocument());

    // Default view: plain Print All, no combine flag.
    fireEvent.click(screen.getByRole('button', { name: /Print All/ }));
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(openSpy.mock.calls[0][0]).not.toContain('combine=1');

    // Multi-Orders view: the print tab is told banners were requested.
    fireEvent.click(screen.getByLabelText('Multi-Orders'));
    await waitFor(() => expect(screen.queryByText('SO-1003')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /Print All/ }));
    expect(openSpy).toHaveBeenCalledTimes(2);
    const multiUrl = openSpy.mock.calls[1][0];
    expect(multiUrl).toContain('combine=1');
    expect(multiUrl).toContain('so_ids=1%2C2');
    openSpy.mockRestore();
  });

  it('shows an empty-state when no order shares an address', async () => {
    apiGetMock.mockImplementation((path = '') => {
      if (path.includes('/admin/sales-orders')) {
        return json({ sales_orders: [QUEUE[2]], total: 1, page: 1, pages: 1, per_page: 200 });
      }
      return json({});
    });
    renderPage();
    await waitFor(() => expect(screen.getByText('SO-1003')).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText('Multi-Orders'));
    await waitFor(() =>
      expect(screen.getByText('No multi-order groups in this queue')).toBeInTheDocument(),
    );
  });
});

describe('TicketDocument combine banner', () => {
  const so = {
    so_id: 1, so_number: 'SO-1001', customer_name: 'Ada Byron',
    shipping_address_line1: '12 Elm St', shipping_address_city: 'Denver',
    shipping_address_state: 'CO', shipping_address_postal_code: '80202',
    ship_by_date: '2026-07-20', order_date: '2026-07-13', ship_method: 'GROUND',
  };

  it('renders a SHIP WITH banner naming siblings and the shipment count', () => {
    const { container } = render(<TicketDocument so={so} lines={[]} combineWith={['SO-1002', 'SO-1003']} />);
    const banner = container.querySelector('.pt-combine');
    expect(banner).toBeTruthy();
    expect(within(banner).getByText('SHIP WITH:')).toBeInTheDocument();
    expect(banner.textContent).toContain('SO-1002, SO-1003');
    expect(banner.textContent).toContain('3 orders, one shipment');
  });

  it('renders no banner when there are no siblings (normal single ticket)', () => {
    const { container } = render(<TicketDocument so={so} lines={[]} />);
    expect(container.querySelector('.pt-combine')).toBeNull();
  });
});

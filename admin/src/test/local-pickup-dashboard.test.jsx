/**
 * Dashboard "Local Pickup" tab: lists the local-pickup worklist via the
 * server-side local_pickup filter, and the red "Picked Up?" button marks an
 * order shipped through the SO edit endpoint's PICKED -> SHIPPED transition.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiGetMock = vi.fn();
const apiPutMock = vi.fn();
vi.mock('../api.js', () => ({
  api: {
    get: (...a) => apiGetMock(...a),
    put: (...a) => apiPutMock(...a),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));
vi.mock('../warehouse.jsx', () => ({ useWarehouse: () => ({ warehouseId: 1 }) }));
vi.mock('../auth.jsx', () => ({
  useAuth: () => ({ user: { role: 'ADMIN', allowed_overrides: [] } }),
}));

import Dashboard from '../pages/Dashboard.jsx';

const json = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

const PICKUP_ORDERS = {
  sales_orders: [
    {
      so_id: 11, so_number: 'SO-PICK-1', customer_name: 'Neal Hoffberg',
      status: 'PICKED', order_date: '2026-06-14T00:00:00Z',
      ship_method: 'Local Pickup (Free)',
      customer_phone: '555-1212', ship_address: '', priority: 0, memo: '',
    },
    {
      so_id: 12, so_number: 'SO-OPEN-1', customer_name: 'Jane Angler',
      status: 'OPEN', order_date: '2026-06-15T00:00:00Z',
      ship_method: 'Will Call - Local',
      customer_phone: '', ship_address: '', priority: 0, memo: '',
    },
  ],
  total: 2, page: 1, pages: 1, per_page: 1000,
};

function wire() {
  apiGetMock.mockImplementation((path = '') => {
    if (path.startsWith('/admin/sales-orders')) return json(PICKUP_ORDERS);
    return json({}); // dashboard preferences / productivity / etc.
  });
  apiPutMock.mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) });
}

async function openLocalPickupTab() {
  const view = render(<MemoryRouter><Dashboard /></MemoryRouter>);
  fireEvent.click(await view.findByText('Local Pickup'));
  return view;
}

describe('Dashboard Local Pickup tab', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPutMock.mockReset();
    wire();
  });

  it('lists local pickup orders via the server-side local_pickup filter', async () => {
    const { findByText } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    await findByText('Neal Hoffberg');
    await findByText('SO-OPEN-1');
    expect(apiGetMock).toHaveBeenCalledWith(
      expect.stringContaining('local_pickup=true'),
      expect.anything(),
    );
  });

  it('passes the status filter through to the request', async () => {
    const { findByText, getByDisplayValue } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    fireEvent.change(getByDisplayValue('All statuses'), { target: { value: 'PICKED' } });
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        expect.stringContaining('status=PICKED'),
        expect.anything(),
      );
    });
  });

  it('sends OPEN,PICKED when the operator picks the Open + Picked view', async () => {
    const { findByText, getByDisplayValue } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    fireEvent.change(getByDisplayValue('All statuses'), { target: { value: 'OPEN,PICKED' } });
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        // comma is percent-encoded by URLSearchParams
        expect.stringContaining('status=OPEN%2CPICKED'),
        expect.anything(),
      );
    });
  });

  it('confirms in a JSX modal (not window.confirm) then ships a PICKED order', async () => {
    const { findByText, getAllByText } = await openLocalPickupTab();
    await findByText('SO-PICK-1');
    fireEvent.click(getAllByText('Picked Up?')[0]);  // first row = PICKED
    // A JSX confirm modal appears; nothing ships until it is confirmed.
    const confirmBtn = await findByText('Yes, mark picked up');
    expect(apiPutMock).not.toHaveBeenCalled();
    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(apiPutMock).toHaveBeenCalledWith(
        '/admin/sales-orders/11',
        { status: 'SHIPPED' },
        expect.anything(),
      );
    });
  });

  it('shows a loud modal (no silent no-op) when an OPEN order cannot be picked up', async () => {
    const { findByText, getAllByText } = await openLocalPickupTab();
    await findByText('SO-OPEN-1');
    fireEvent.click(getAllByText('Picked Up?')[1]);  // second row = OPEN
    await findByText('Not ready for pickup');
    expect(apiPutMock).not.toHaveBeenCalled();
  });
});

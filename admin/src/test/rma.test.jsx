/**
 * RMA admin page: list the <orig>-RMA return SOs, open one, and receive a
 * line into a chosen disposition (warehouse + bin) via receive-return.
 *
 * Locks:
 *   - List loads return SOs on mount (order_type=return).
 *   - Opening an RMA loads its lines + warehouses + bins for the disposition.
 *   - Receiving a line POSTs receive-return with the chosen qty + disposition.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();

vi.mock('../api.js', () => ({
  api: {
    get: (...args) => apiGetMock(...args),
    post: (...args) => apiPostMock(...args),
    patch: (...args) => apiPatchMock(...args),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

// RMA.jsx reads the current user (useAuth) to ADMIN-gate the Delete button.
// Mock it like the other admin-page tests so the component renders under test.
vi.mock('../auth.jsx', () => ({
  useAuth: () => ({ user: { role: 'ADMIN', allowed_overrides: [] } }),
}));

import RMA from '../pages/RMA.jsx';

function jsonResponse(body, ok = true) {
  return Promise.resolve({
    ok,
    status: ok ? 200 : 400,
    json: () => Promise.resolve(body),
  });
}

const RMA_ROW = {
  so_id: 5,
  so_number: 'POS-5-RMA',
  customer_name: 'Jane Angler',
  status: 'OPEN',
  order_type: 'return',
  parent_so_id: 1,
  warehouse_id: 1,
  created_at: '2026-06-08T12:00:00',
};

const RMA_DETAIL = {
  sales_order: { so_id: 5, so_number: 'POS-5-RMA', status: 'OPEN', customer_name: 'Jane Angler', warehouse_id: 1 },
  lines: [
    { so_line_id: 11, item_id: 42, sku: 'ROD-100', item_name: 'Rod', quantity_ordered: 2, quantity_received: 0 },
  ],
};

function wireDefaults() {
  apiGetMock.mockImplementation((path) => {
    if (path.startsWith('/admin/sales-orders?')) {
      return jsonResponse({ sales_orders: [RMA_ROW] });
    }
    if (path === '/admin/sales-orders/5') {
      return jsonResponse(RMA_DETAIL);
    }
    if (path.startsWith('/admin/warehouses')) {
      return jsonResponse({
        warehouses: [
          { warehouse_id: 1, warehouse_code: 'SELLABLE', warehouse_name: 'Sellable' },
          { warehouse_id: 2, warehouse_code: 'DEFECTIVE', warehouse_name: 'Defective' },
        ],
      });
    }
    if (path.startsWith('/admin/bins')) {
      return jsonResponse({ bins: [{ bin_id: 10, bin_code: 'A-1', bin_type: 'Pickable' }] });
    }
    return jsonResponse({});
  });
  apiPostMock.mockReturnValue(
    jsonResponse({ message: 'Return received', inventory_adjustment_id: 1, quantity_on_hand: 50 }),
  );
  apiPatchMock.mockReturnValue(jsonResponse({ so_id: 5, memo: 'note' }));
}

describe('RMA admin page', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    apiPatchMock.mockReset();
    wireDefaults();
  });

  it('lists return SOs on mount', async () => {
    const { getByText } = render(
      <MemoryRouter><RMA /></MemoryRouter>,
    );
    await waitFor(() => expect(getByText('POS-5-RMA')).toBeInTheDocument());
    // The list query scopes to return SOs.
    expect(
      apiGetMock.mock.calls.some(
        ([p]) => p.includes('/admin/sales-orders?') && p.includes('order_type=return'),
      ),
    ).toBe(true);
  });

  it('opens an RMA and receives a line into the chosen disposition', async () => {
    const { getByText, getByTestId } = render(
      <MemoryRouter><RMA /></MemoryRouter>,
    );
    await waitFor(() => expect(getByText('POS-5-RMA')).toBeInTheDocument());

    fireEvent.click(getByText('POS-5-RMA'));

    // Detail loads the line + the disposition pickers.
    await waitFor(() => expect(getByTestId('rma-qty-ROD-100')).toBeInTheDocument());
    expect(getByTestId('rma-disposition-warehouse')).toBeInTheDocument();

    // Default disposition is the RMA's warehouse (id 1) with its first bin.
    fireEvent.change(getByTestId('rma-qty-ROD-100'), { target: { value: '2' } });
    fireEvent.click(getByTestId('rma-receive-ROD-100'));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalled());
    const [path, body] = apiPostMock.mock.calls[0];
    expect(path).toBe('/admin/sales-orders/5/receive-return');
    expect(body).toEqual({
      item_id: 42,
      quantity: 2,
      warehouse_id: 1,
      bin_id: 10,
    });
  });

  it('saves an operator note on the RMA via the memo PATCH', async () => {
    const { getByText, getByTestId } = render(
      <MemoryRouter><RMA /></MemoryRouter>,
    );
    await waitFor(() => expect(getByText('POS-5-RMA')).toBeInTheDocument());
    fireEvent.click(getByText('POS-5-RMA'));
    await waitFor(() => expect(getByTestId('rma-memo')).toBeInTheDocument());

    fireEvent.change(getByTestId('rma-memo'), { target: { value: 'customer kept the reel' } });
    fireEvent.click(getByTestId('rma-memo-save'));

    await waitFor(() => expect(apiPatchMock).toHaveBeenCalled());
    const [path, body] = apiPatchMock.mock.calls[0];
    expect(path).toBe('/admin/sales-orders/5/memo');
    expect(body).toEqual({ memo: 'customer kept the reel' });
  });
});

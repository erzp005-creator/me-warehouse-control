/**
 * SO-modal fulfillment-action gates by order_type.
 *
 * The Partially Fulfill / Admin Pick / Release Picked Quantities buttons
 * mirror the backend rule: available on every order_type EXCEPT return,
 * and Partially Fulfill additionally hides on backorder (the one-level
 * chaining cap). replacement/exchange children -- which carry a
 * parent_so_id -- are now eligible (they used to be blocked by the
 * parent_so_id cap). The modal is opened via the ?focus= deep-link.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
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

function wire({ order_type, status = 'OPEN', pick_tasks = [] }) {
  const so = {
    so_id: 42, so_number: 'SO-GATE-42', customer_name: 'Cust',
    status, order_type,
    // Children (replacement/exchange/backorder/return) carry a parent.
    parent_so_id: order_type === 'sale' ? null : 1,
    order_date: '2026-06-14T00:00:00Z', ship_method: 'GROUND',
    customer_phone: '', customer_email: '', ship_address: '',
    priority: 0, memo: '',
  };
  apiGetMock.mockImplementation((path = '') => {
    if (path.includes('/admin/sales-orders/42')) {
      return json({ sales_order: so, lines: [], pick_tasks });
    }
    if (path.includes('/admin/sales-orders')) {
      return json({
        sales_orders: [so], total: 1, page: 1, pages: 1, per_page: 1000,
      });
    }
    return json({});
  });
}

async function openModal(cfg) {
  wire(cfg);
  render(
    <MemoryRouter initialEntries={['/sales-orders?focus=SO-GATE-42']}>
      <SalesOrders />
    </MemoryRouter>,
  );
  // The ?focus effect auto-opens the edit modal; wait for its footer.
  await screen.findByText('Save');
}

describe('SalesOrders fulfillment-action gates by order_type', () => {
  beforeEach(() => { apiGetMock.mockReset(); });

  it.each(['sale', 'replacement', 'exchange'])(
    'shows Partially Fulfill + Admin Pick for an OPEN %s',
    async (order_type) => {
      await openModal({ order_type });
      expect(screen.queryByText('Partially Fulfill')).toBeTruthy();
      expect(screen.queryByText('Admin Pick')).toBeTruthy();
    },
  );

  it('hides Partially Fulfill for a backorder but keeps Admin Pick', async () => {
    await openModal({ order_type: 'backorder' });
    expect(screen.queryByText('Partially Fulfill')).toBeNull();
    expect(screen.queryByText('Admin Pick')).toBeTruthy();
  });

  it('hides all three fulfillment actions for a return', async () => {
    await openModal({
      order_type: 'return',
      pick_tasks: [{ pick_task_id: 1, quantity_picked: 1 }],
    });
    expect(screen.queryByText('Partially Fulfill')).toBeNull();
    expect(screen.queryByText('Admin Pick')).toBeNull();
    expect(screen.queryByText('Release Picked Quantities')).toBeNull();
  });

  it('shows Release Picked Quantities for a non-return SO with picks', async () => {
    await openModal({
      order_type: 'exchange', status: 'PICKED',
      pick_tasks: [{ pick_task_id: 1, quantity_picked: 1 }],
    });
    expect(screen.queryByText('Release Picked Quantities')).toBeTruthy();
  });
});

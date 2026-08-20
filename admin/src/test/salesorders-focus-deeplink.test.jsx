/**
 * ?focus= deep-link destination.
 *
 * Clicking a row on the Sales Orders page has always opened the read-only
 * order view. Arriving at the same page via ?focus= opened the EDIT form
 * instead: same apparent action, two destinations, and the deep-link
 * reached the more dangerous one.
 *
 * It is also the mechanism behind the separate report that order notes
 * "only show up if you edit the order". The read-only view renders the
 * memo; operators arriving from a Teams adaptive card never saw it because
 * they landed in a textarea.
 *
 * Tests pin that ?focus= resolves to the view, and that the pencil still
 * reaches the edit form.
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

const SO = {
  so_id: 77, so_number: 'SO-FOCUS-77', customer_name: 'Ada Lovelace',
  status: 'OPEN', order_type: 'sale', parent_so_id: null,
  order_date: '2026-08-01T00:00:00Z', ship_method: 'GROUND',
  customer_phone: '', customer_email: '', ship_address: '',
  priority: 0, memo: 'Leave at the back door',
};

function wire() {
  apiGetMock.mockImplementation((path = '') => {
    if (/\/admin\/sales-orders\/\d+\/related/.test(path)) {
      return json({ so_id: 77, related_count: 0, records: [] });
    }
    if (/\/admin\/sales-orders\/\d+$/.test(path)) {
      return json({ sales_order: SO, lines: [], pick_tasks: [] });
    }
    if (path.includes('/admin/sales-orders')) {
      return json({ sales_orders: [SO], total: 1, page: 1, pages: 1, per_page: 50 });
    }
    if (path.includes('/admin/source-systems')) return json({ source_systems: [] });
    return json({});
  });
}

function renderAt(entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}><SalesOrders /></MemoryRouter>,
  );
}

describe('?focus= deep-link', () => {
  beforeEach(() => { apiGetMock.mockReset(); });

  it('opens the read-only view, not the edit form', async () => {
    wire();
    renderAt('/sales-orders?focus=SO-FOCUS-77');

    await waitFor(() => expect(screen.getByText('Order Summary')).toBeTruthy());
    // The edit form's Save button is the tell; it must not be here.
    expect(screen.queryByText('Save')).toBeNull();
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('SO SO-FOCUS-77');
  });

  it('shows the order note without the operator entering edit mode', async () => {
    // The "notes only show if you edit the order" complaint: the view has
    // always rendered the memo, the deep-link just never landed there.
    wire();
    renderAt('/sales-orders?focus=SO-FOCUS-77');

    await waitFor(() => expect(screen.getByText('Order Summary')).toBeTruthy());
    expect(screen.getByText('Leave at the back door')).toBeTruthy();
    expect(screen.queryByText('Save')).toBeNull();
  });

  it('matches so_number case-insensitively', async () => {
    wire();
    renderAt('/sales-orders?focus=so-focus-77');
    await waitFor(() => expect(screen.getByText('Order Summary')).toBeTruthy());
  });

  it('does nothing when focus matches no order', async () => {
    apiGetMock.mockImplementation((path = '') => {
      if (path.includes('/admin/sales-orders')) {
        return json({ sales_orders: [], total: 0, page: 1, pages: 1, per_page: 50 });
      }
      return json({});
    });
    renderAt('/sales-orders?focus=SO-NOPE');
    await waitFor(() => expect(apiGetMock).toHaveBeenCalled());
    expect(screen.queryByText('Order Summary')).toBeNull();
    expect(screen.queryByText('Save')).toBeNull();
  });

  it('the row pencil still reaches the edit form', async () => {
    wire();
    renderAt('/sales-orders');
    const pencil = await screen.findByLabelText('Edit');

    await act(async () => { fireEvent.click(pencil); });

    await waitFor(() => expect(screen.getByText('Save')).toBeTruthy());
  });

  it('a plain row click opens the view, same as the deep-link', async () => {
    wire();
    renderAt('/sales-orders');
    const cell = await screen.findByText('SO-FOCUS-77');

    await act(async () => { fireEvent.click(cell); });

    await waitFor(() => expect(screen.getByText('Order Summary')).toBeTruthy());
    expect(screen.queryByText('Save')).toBeNull();
  });
});

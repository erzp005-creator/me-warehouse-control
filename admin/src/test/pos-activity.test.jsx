/**
 * POS Activity dashboard renders the wired KPIs, the today-vs-yesterday
 * pace curve, the weekly trend, the channel/tender splits, and the order
 * list -- and degrades gracefully when there's no revenue.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiGetMock = vi.fn();
vi.mock('../api.js', () => ({
  api: { get: (...a) => apiGetMock(...a), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock('../warehouse.jsx', () => ({ warehouseRequired: () => null }));

import POSActivity from '../pages/POSActivity.jsx';

const json = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });

function cumulative(toHour, peak) {
  return Array.from({ length: 24 }, (_, h) => ({
    hour: h,
    cents: h < 9 ? 0 : h >= toHour ? peak : Math.round((peak * (h - 9)) / (toHour - 9)),
  }));
}
function weekDays(values, todayIdx) {
  return values.map((v, i) => ({ date: `2026-06-0${i + 1}`, dow: i, net_cents: v, is_today: i === todayIdx, is_future: i > todayIdx }));
}

const SUMMARY = {
  tz: 'America/Denver', date: '2026-06-12', is_today: true, current_hour: 15,
  today: {
    net_cents: 1122227, sales_count: 96, sales_total_cents: 1187966,
    refund_count: 8, refund_total_cents: 65739, avg_sale_cents: 12375,
    counter_cents: 900000, counter_count: 80, phone_cents: 287966, phone_count: 16,
  },
  yesterday: { net_cents: 980000 },
  vs_yesterday_cents: 142227,
  pace: { today: cumulative(15, 1122227), yesterday: cumulative(20, 980000) },
  week: {
    this: weekDays([120000, 150000, 90000, 1122227, 0, 0, 0], 3),
    last: weekDays([110000, 130000, 100000, 95000, 140000, 160000, 80000], -1),
    this_total_cents: 1482227, last_total_cents: 715000,
  },
  tenders: [{ method: 'card', cents: 800000, count: 60 }, { method: 'cash', cents: 387966, count: 36 }],
  active_terminals: ['REG-01', 'REG-02'],
};

const ORDERS = {
  sales_orders: [{
    so_id: 1, so_number: 'POS-9001', status: 'SHIPPED', order_type: 'sale',
    channel: 'Phone Order', created_at: '2026-06-12T19:00:00Z', customer_name: 'Jane Doe',
    total_cents: 2482, terminal_id: 'REG-01', payment_method: 'card', external_txn_ref: '0000abcd',
  }],
  total: 1, page: 1, pages: 1, per_page: 50,
};

function wire(summary) {
  apiGetMock.mockImplementation((path = '') => {
    if (path.startsWith('/admin/pos/summary')) return json(summary);
    if (path.startsWith('/admin/pos/sales-orders')) return json(ORDERS);
    return json({});
  });
}

describe('POSActivity', () => {
  beforeEach(() => apiGetMock.mockReset());

  it('renders KPIs, pace curve, weekly trend, splits, and orders', async () => {
    wire(SUMMARY);
    const { container, findByText, getAllByText } = render(<MemoryRouter><POSActivity /></MemoryRouter>);

    await findByText('$11,222.27');               // net revenue hero
    await findByText('Phone Orders');              // channel KPI (unique; filter says "Phone Order")
    await findByText('This Week');
    await findByText('Avg Sale');
    await waitFor(() => {
      expect(container.querySelectorAll('svg').length).toBeGreaterThanOrEqual(2); // pace + week charts
      expect(container.querySelectorAll('.pos-bar').length).toBeGreaterThan(0);   // weekly bars
      expect(container.querySelectorAll('.pos-splitseg').length).toBeGreaterThan(0); // splits
    });
    await findByText('POS-9001');
    expect(getAllByText(/vs\. yesterday|vs last week/).length).toBeGreaterThan(0);
  });

  it('degrades gracefully with no revenue', async () => {
    wire({
      ...SUMMARY,
      today: { net_cents: 0, sales_count: 0, refund_count: 0, avg_sale_cents: 0, counter_cents: 0, phone_cents: 0, counter_count: 0, phone_count: 0, sales_total_cents: 0, refund_total_cents: 0 },
      vs_yesterday_cents: 0,
      pace: { today: cumulative(24, 0), yesterday: cumulative(24, 0) },
      week: { this: weekDays([0, 0, 0, 0, 0, 0, 0], 3), last: weekDays([0, 0, 0, 0, 0, 0, 0], -1), this_total_cents: 0, last_total_cents: 0 },
      tenders: [],
      active_terminals: [],
    });
    const { findByText } = render(<MemoryRouter><POSActivity /></MemoryRouter>);
    await findByText('No POS revenue today or yesterday.');
    await findByText('No revenue this week or last.');
  });
});

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SiteGiantWorkload } from '../pages/WorkControl.jsx';

describe('SiteGiant workload panel', () => {
  it('keeps package workload separate from warehouse execution', () => {
    render(<SiteGiantWorkload workload={{
      latest: {
        snapshot_id: 2,
        captured_at: '2026-08-25T05:00:00Z',
        period_label: 'From 19 Aug 2026 to 25 Aug 2026',
        pending_packages: 5,
        to_process_packages: 164,
        printed_packages: 1527,
        pending_pickup_packages: 0,
        remaining_packages: 169,
        unprocessed_percent: 10,
      },
      snapshots: [{
        snapshot_id: 2,
        captured_at: '2026-08-25T05:00:00Z',
        remaining_packages: 169,
      }],
      task_progress: [
        { task_type: 'PICKING', status: 'QUEUED', task_count: 3, order_count: 150 },
        { task_type: 'PICKING', status: 'COMPLETED', task_count: 2, order_count: 90 },
      ],
      sync: { status: 'current', age_minutes: 4 },
      change: { remaining_packages: -14, printed_packages: 14 },
    }} />);

    expect(screen.getAllByText('169')).toHaveLength(2);
    expect(screen.getByText('packages · 10% of visible pipeline')).toBeInTheDocument();
    expect(screen.getByText((_, element) => element.tagName === 'SPAN' && element.textContent === '3 open tasks')).toBeInTheDocument();
    expect(screen.getByText((_, element) => element.tagName === 'SPAN' && element.textContent === '90 orders done')).toBeInTheDocument();
    expect(screen.getByText(/SiteGiant reports packages/)).toBeInTheDocument();
  });

  it('shows setup state without blocking the task queue', () => {
    render(<SiteGiantWorkload workload={{
      latest: null,
      snapshots: [],
      task_progress: [],
      sync: { status: 'missing', age_minutes: null },
      change: {},
    }} />);

    expect(screen.getByText('No SiteGiant snapshot has arrived.')).toBeInTheDocument();
    expect(screen.getByText(/task queue continues to work normally/i)).toBeInTheDocument();
  });
});

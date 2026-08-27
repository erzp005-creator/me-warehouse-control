import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DispatchBoard, SiteGiantWorkload } from '../pages/WorkControl.jsx';

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
      forecast: {
        unprinted_packages: 169,
        pack_note_capacity: 50,
        estimated_pack_notes: 4,
        estimated_picking_minutes: 102,
        estimated_packing_minutes: 136,
        estimated_total_labor_minutes: 237,
        estimated_one_picker_one_packer_minutes: 166,
        rates: {
          PICKING: { minutes_per_50: 30, source: 'baseline' },
          PACKING: { minutes_per_50: 40, source: 'baseline' },
        },
        history_threshold: { completed_tasks: 5, completed_orders: 100, lookback_days: 30 },
      },
      sync: { status: 'current', age_minutes: 4 },
      change: { remaining_packages: -14, printed_packages: 14 },
    }} />);

    expect(screen.getAllByText('169')).toHaveLength(2);
    expect(screen.getByText('packages · 10% of visible pipeline')).toBeInTheDocument();
    expect(screen.getByText((_, element) => element.tagName === 'SPAN' && element.textContent === '3 open tasks')).toBeInTheDocument();
    expect(screen.getByText((_, element) => element.tagName === 'SPAN' && element.textContent === '90 orders done')).toBeInTheDocument();
    expect(screen.getByText('Estimated Pack Notes')).toBeInTheDocument();
    expect(screen.getByText('2h 46m')).toBeInTheDocument();
    expect(screen.getByText(/3h 57m total labour/)).toBeInTheDocument();
    expect(screen.getByText(/supervisor baselines/)).toBeInTheDocument();
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

describe('automatic dispatch workload panel', () => {
  it('shows employee capacity, current work and manual override controls', () => {
    render(<DispatchBoard
      loading={false}
      onRun={() => {}}
      onAvailability={() => {}}
      onAssign={() => {}}
      data={{
        summary: {
          available_workers: 3,
          total_workers: 4,
          scheduled_minutes: 145,
          unassigned_tasks: 1,
          unassigned_minutes: 20,
          estimated_clear_minutes: 65,
        },
        policy: { picking_minutes_per_50: 30, packing_minutes_per_50: 40 },
        workers: [{
          user_id: 1,
          username: 'mong',
          full_name: 'Mong',
          availability_status: 'AVAILABLE',
          daily_capacity_minutes: 480,
          allowed_task_types: ['PICKING', 'PACKING', 'RECEIVING'],
          scheduled_minutes: 65,
          capacity_percent: 13.5,
          scheduled_task_count: 2,
          completed_tasks_today: 3,
          current_task: {
            task_id: 8, task_type: 'PICKING', reference: '3440',
            remaining_minutes: 21,
          },
          next_tasks: [{
            task_id: 9, task_type: 'PACKING', reference: '3441',
            remaining_minutes: 40, estimated_minutes: 40, order_count: 50,
          }],
        }],
        unassigned_tasks: [{
          task_id: 10, task_type: 'RECEIVING', reference: 'DO-22',
          estimated_minutes: 20, unit_count: 100,
        }],
      }}
    />);

    expect(screen.getByRole('heading', { name: "Today's employee load" })).toBeInTheDocument();
    expect(screen.getByText('Mong')).toBeInTheDocument();
    expect(screen.getByText(/Picking ·/)).toHaveTextContent('3440');
    expect(screen.getByRole('button', { name: 'Balance waiting tasks' })).toBeEnabled();
    expect(screen.getByLabelText('Mong availability')).toHaveValue('AVAILABLE');
    expect(screen.getByLabelText('Assign task 10')).toBeInTheDocument();
    expect(screen.getByText(/No KPI score or ranking/i)).toBeInTheDocument();
  });
});

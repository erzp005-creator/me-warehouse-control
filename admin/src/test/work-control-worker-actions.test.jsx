import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PersonalWorkSummary, QueueView } from '../pages/WorkControl.jsx';
import { prepareReceivingEntry } from '../pages/workControlReceiving.js';
import { prepareWorkIssue } from '../pages/workControlIssues.js';

const baseTask = {
  task_id: 7,
  task_type: 'PICKING',
  pack_note_ref: '3443',
  claimed_by: 'mong',
  assigned_to: 'mong',
  order_count: 50,
  unit_count: 0,
  active_seconds: 0,
  paused_seconds: 0,
};

function renderQueue(task, overrides = {}) {
  const actions = {
    onClaim: vi.fn(),
    onStart: vi.fn(),
    onCount: vi.fn(),
    onWorkerScan: vi.fn(),
    onWorkerComplete: vi.fn(),
    onWorkerPause: vi.fn(),
    onWorkerResume: vi.fn(),
    onWorkerReport: vi.fn(),
    ...overrides,
  };
  render(
    <QueueView
      tasks={[task]}
      workload={{}}
      showWorkload={false}
      currentUser={{ username: 'mong', role: 'USER' }}
      {...actions}
    />,
  );
  return actions;
}

describe('employee Work Control actions', () => {
  it('offers scan-to-start for a claimed picking task', () => {
    const actions = renderQueue({ ...baseTask, status: 'CLAIMED' });

    fireEvent.click(screen.getByRole('button', { name: 'Scan to start' }));
    expect(actions.onWorkerScan).toHaveBeenCalledWith(expect.objectContaining({ task_id: 7 }));
  });

  it('offers complete, pause and issue reporting for active work', () => {
    const actions = renderQueue({ ...baseTask, status: 'IN_PROGRESS' });

    fireEvent.click(screen.getByRole('button', { name: '100% complete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    fireEvent.click(screen.getByRole('button', { name: 'Report issue' }));
    expect(actions.onWorkerComplete).toHaveBeenCalledWith(expect.objectContaining({ task_id: 7 }));
    expect(actions.onWorkerPause).toHaveBeenCalledWith(expect.objectContaining({ task_id: 7 }));
    expect(actions.onWorkerReport).toHaveBeenCalledWith(expect.objectContaining({ task_id: 7 }));
  });

  it('offers resume for paused work', () => {
    const actions = renderQueue({ ...baseTask, status: 'PAUSED' });

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(actions.onWorkerResume).toHaveBeenCalledWith(expect.objectContaining({ task_id: 7 }));
  });

  it('does not show employee execution controls to an admin', () => {
    render(
      <QueueView
        tasks={[{ ...baseTask, status: 'CLAIMED' }]}
        workload={{}}
        showWorkload={false}
        currentUser={{ username: 'admin', role: 'ADMIN' }}
      />,
    );

    expect(screen.queryByRole('button', { name: 'Scan to start' })).not.toBeInTheDocument();
  });
});

describe('employee issue validation', () => {
  it('ties the report to the current task and keeps responsibility unconfirmed', () => {
    const result = prepareWorkIssue({ ...baseTask, batch_id: 3 }, 1, {
      error_type: 'wrong_quantity',
      order_reference: 'MY-CARRIER-00001',
      sku: 'RB-001',
      quantity: '2',
      description: 'Cross-check found two units missing',
    });

    expect(result.error).toBeUndefined();
    expect(result.payload).toMatchObject({
      warehouse_id: 1,
      task_id: 7,
      batch_id: 3,
      error_type: 'WRONG_QUANTITY',
      severity: 'MEDIUM',
      discovered_stage: 'PICKING',
      courier_barcode: 'MY-CARRIER-00001',
      sku: 'RB-001',
      quantity: 2,
    });
    expect(result.payload).not.toHaveProperty('responsibility');
  });

  it('requires a description and a valid whole quantity', () => {
    expect(prepareWorkIssue(baseTask, 1, {
      error_type: 'WRONG_ITEM', quantity: '', description: '',
    }).error).toMatch(/describe what happened/i);
    expect(prepareWorkIssue(baseTask, 1, {
      error_type: 'WRONG_ITEM', quantity: '1.5', description: 'Wrong SKU in tote',
    }).error).toMatch(/whole number/i);
  });
});

describe('personal work record', () => {
  const report = {
    employee: 'mong',
    full_name: 'Mong',
    scoring_applied: false,
    ranking_applied: false,
    periods: {
      today: {
        range: { start: '2026-08-27', end: '2026-08-27' },
        summary: {
          completed_tasks: 2,
          orders_handled: 90,
          active_seconds: 2400,
          paused_seconds: 300,
          confirmed_mistakes: 1,
          reported_issues: 2,
          pending_reported_issues: 1,
        },
        activity: [{
          task_type: 'PICKING', completed_tasks: 2, orders_handled: 90,
          skus_handled: 0, units_handled: 0, active_seconds: 2400,
          average_active_seconds: 1200,
        }],
        recent: [],
      },
      week: {
        range: { start: '2026-08-24', end: '2026-08-27' },
        summary: { completed_tasks: 7, orders_handled: 320 },
        activity: [],
        recent: [],
      },
    },
  };

  it('shows objective personal facts without a score or ranking', () => {
    render(<PersonalWorkSummary report={report} period="today" onPeriodChange={vi.fn()} />);

    expect(screen.getByRole('heading', { name: 'My work record' })).toBeInTheDocument();
    expect(screen.getByText('90')).toBeInTheDocument();
    expect(screen.getByText('Picking')).toBeInTheDocument();
    expect(screen.getByText(/No KPI score, commission formula or staff ranking/i)).toBeInTheDocument();
  });

  it('lets the employee choose this week without exposing another employee', () => {
    const onPeriodChange = vi.fn();
    render(<PersonalWorkSummary report={report} period="today" onPeriodChange={onPeriodChange} />);

    fireEvent.click(screen.getByRole('button', { name: 'This week' }));
    expect(onPeriodChange).toHaveBeenCalledWith('week');
    expect(screen.queryByText(/Annie|Cherry/i)).not.toBeInTheDocument();
  });
});

describe('multi-SKU receiving entry validation', () => {
  const selected = { sku: 'RB-001', item_name: 'Roller Balloon' };
  const photo = new File(['photo'], 'arrival.jpg', { type: 'image/jpeg' });

  it('requires an explicit received quantity and one photo per SKU', () => {
    expect(prepareReceivingEntry(selected, { expected: '', received: '', damaged: '0', note: '' }, photo).error)
      .toMatch(/received quantity/i);
    expect(prepareReceivingEntry(selected, { expected: '10', received: '10', damaged: '0', note: '' }, null).error)
      .toMatch(/one arrival photo/i);
  });

  it('builds a balanced receiving line with discrepancy notes', () => {
    const result = prepareReceivingEntry(
      selected,
      { expected: '12', received: '10', damaged: '2', note: 'Two damaged cartons' },
      photo,
    );

    expect(result.error).toBeUndefined();
    expect(result.entry).toMatchObject({
      sku: 'RB-001',
      expected_quantity: 12,
      received_quantity: 10,
      good_quantity: 8,
      damaged_quantity: 2,
      notes: 'Two damaged cartons',
      photo,
    });
  });

  it('rejects damaged quantity above received quantity', () => {
    const result = prepareReceivingEntry(
      selected,
      { expected: '', received: '3', damaged: '4', note: '' },
      photo,
    );
    expect(result.error).toMatch(/between 0 and the received quantity/i);
  });
});


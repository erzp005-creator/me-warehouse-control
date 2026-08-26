import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { QueueView } from '../pages/WorkControl.jsx';

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

  it('offers complete and pause for active work', () => {
    const actions = renderQueue({ ...baseTask, status: 'IN_PROGRESS' });

    fireEvent.click(screen.getByRole('button', { name: '100% complete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(actions.onWorkerComplete).toHaveBeenCalledWith(expect.objectContaining({ task_id: 7 }));
    expect(actions.onWorkerPause).toHaveBeenCalledWith(expect.objectContaining({ task_id: 7 }));
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


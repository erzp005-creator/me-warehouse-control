/**
 * StatusTag: REFUNDED (mig 074) renders as its own colour, visually
 * distinct from CANCELLED, so operators can tell a refund from a cancel.
 */

import React from 'react';
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import StatusTag from '../components/StatusTag.jsx';

describe('StatusTag REFUNDED', () => {
  it('renders REFUNDED with a distinct (non-cancelled) colour', () => {
    const { getByText } = render(<StatusTag status="REFUNDED" />);
    const el = getByText('REFUNDED');
    expect(el.className).toContain('tag-warning');
    expect(el.className).not.toContain('tag-gray');
  });

  it('still renders CANCELLED as gray', () => {
    const { getByText } = render(<StatusTag status="CANCELLED" />);
    expect(getByText('CANCELLED').className).toContain('tag-gray');
  });
});

import { describe, it, expect } from 'vitest';
import {
  carrierFromShipMethod,
  normalizeCarrier,
  shipMethodDisplay,
} from '../utils/shipMethod.js';

describe('carrierFromShipMethod', () => {
  it('maps USPS-named services (incl. ones that omit "usps")', () => {
    expect(carrierFromShipMethod('USPS Ground Advantage')).toBe('USPS');
    expect(carrierFromShipMethod('USPS Priority Mail')).toBe('USPS');
    expect(carrierFromShipMethod('Ground Advantage')).toBe('USPS');
    expect(carrierFromShipMethod('First Class Package')).toBe('USPS');
    expect(carrierFromShipMethod('Media Mail')).toBe('USPS');
  });

  it('maps UPS and FedEx without colliding with USPS', () => {
    expect(carrierFromShipMethod('UPS Ground')).toBe('UPS');
    expect(carrierFromShipMethod('FedEx 2 Day')).toBe('FEDEX');
    // 'usps' must not be read as containing 'ups'.
    expect(carrierFromShipMethod('USPS')).toBe('USPS');
  });

  it('returns null when the method names no carrier', () => {
    expect(carrierFromShipMethod('Local Pickup (Free)')).toBeNull();
    expect(carrierFromShipMethod('Standard Shipping (Economy 4-5 days)')).toBeNull();
    expect(carrierFromShipMethod('')).toBeNull();
    expect(carrierFromShipMethod(null)).toBeNull();
  });
});

describe('normalizeCarrier', () => {
  it('canonicalizes known carriers', () => {
    expect(normalizeCarrier('UPS')).toBe('UPS');
    expect(normalizeCarrier('usps')).toBe('USPS');
    expect(normalizeCarrier('FedEx')).toBe('FEDEX');
  });

  it('passes unknown carriers through and nulls blanks', () => {
    expect(normalizeCarrier('Other')).toBe('OTHER');
    expect(normalizeCarrier('')).toBeNull();
    expect(normalizeCarrier(null)).toBeNull();
  });
});

describe('shipMethodDisplay', () => {
  it('annotates a USPS-named method that shipped UPS (the reported bug)', () => {
    const d = shipMethodDisplay({ ship_method: 'USPS Ground Advantage', carrier: 'UPS', tracking_number: '1Z3Y4A390321365319' });
    expect(d.diverged).toBe(true);
    expect(d.text).toBe('USPS Ground Advantage (shipped UPS)');
  });

  it('leaves a matching carrier untouched', () => {
    expect(shipMethodDisplay({ ship_method: 'USPS Ground Advantage', carrier: 'USPS' }))
      .toEqual({ text: 'USPS Ground Advantage', diverged: false });
    expect(shipMethodDisplay({ ship_method: 'UPS Ground', carrier: 'UPS' }))
      .toEqual({ text: 'UPS Ground', diverged: false });
  });

  it('does not annotate an unshipped order (no carrier yet)', () => {
    expect(shipMethodDisplay({ ship_method: 'USPS Ground Advantage', carrier: null }))
      .toEqual({ text: 'USPS Ground Advantage', diverged: false });
  });

  it('does not annotate a generic method name that claims no carrier', () => {
    expect(shipMethodDisplay({ ship_method: 'Standard Shipping (Economy 4-5 days)', carrier: 'UPS' }))
      .toEqual({ text: 'Standard Shipping (Economy 4-5 days)', diverged: false });
  });

  it('falls back when no method is present, and tolerates a null order', () => {
    expect(shipMethodDisplay({ ship_method: '', carrier: 'UPS' }).text).toBe('-');
    expect(shipMethodDisplay(null).text).toBe('-');
    expect(shipMethodDisplay(undefined, { fallback: 'n/a' }).text).toBe('n/a');
  });
});

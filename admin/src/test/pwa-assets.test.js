import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('warehouse PWA shell', () => {
  it('opens Work Control as the installed start screen', () => {
    const manifest = JSON.parse(readFileSync('public/manifest.webmanifest', 'utf8'));
    expect(manifest.start_url).toBe('/work-control');
    expect(manifest.display).toBe('standalone');
    expect(manifest.icons.length).toBeGreaterThan(0);
  });

  it('never caches authenticated API responses', () => {
    const serviceWorker = readFileSync('public/sw.js', 'utf8');
    expect(serviceWorker).toContain("url.pathname.startsWith('/api/')");
    expect(serviceWorker).toContain("request.method !== 'GET'");
  });
});

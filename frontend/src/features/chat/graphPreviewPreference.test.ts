import { describe, expect, it } from 'vitest';
import { graphPreviewPreference } from './graphPreviewPreference';

describe('graph review preference', () => {
  it('defaults to review unless the deployment disables it', () => {
    expect(graphPreviewPreference(null, 'true')).toBe(true);
    expect(graphPreviewPreference(null, 'false')).toBe(false);
  });
  it('keeps an explicit user choice in either deployment', () => {
    expect(graphPreviewPreference('true', 'false')).toBe(true);
    expect(graphPreviewPreference('false', 'true')).toBe(false);
  });
});

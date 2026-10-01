import { describe, expect, it } from 'vitest';
import { graphPreviewPreference } from './graphPreviewPreference';

describe('graph review preference', () => {
  it('defaults to review unless the deployment disables it', () => {
    expect(graphPreviewPreference(null, null, 'true')).toBe(true);
    expect(graphPreviewPreference(null, null, 'false')).toBe(false);
  });
  it('applies changed defaults to older browsers and when restoring review', () => {
    expect(graphPreviewPreference('true', null, 'false')).toBe(false);
    expect(graphPreviewPreference('true', 'true', 'false')).toBe(false);
    expect(graphPreviewPreference('false', 'false', 'true')).toBe(true);
  });
  it('keeps subsequent user choices while the deployment default is unchanged', () => {
    expect(graphPreviewPreference('true', 'false', 'false')).toBe(true);
    expect(graphPreviewPreference('false', 'true', 'true')).toBe(false);
    expect(graphPreviewPreference('false', null, 'true')).toBe(false);
  });
});

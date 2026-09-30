export function graphPreviewPreference(saved: string | null, deploymentDefault = import.meta.env.VITE_REVIEW_AI_CHANGES_DEFAULT): boolean {
  if (saved === 'true' || saved === 'false') return saved === 'true';
  return deploymentDefault !== 'false';
}

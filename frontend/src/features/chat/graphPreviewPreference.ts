export function graphPreviewDefault(deploymentDefault = import.meta.env.VITE_REVIEW_AI_CHANGES_DEFAULT): boolean {
  return deploymentDefault !== 'false';
}

export function graphPreviewPreference(saved: string | null, savedDefault: string | null, deploymentDefault = import.meta.env.VITE_REVIEW_AI_CHANGES_DEFAULT): boolean {
  const currentDefault = graphPreviewDefault(deploymentDefault);
  // Older builds always used true. Apply a changed deployment default once,
  // then retain any subsequent user choice while that default stays the same.
  if ((savedDefault !== 'false') !== currentDefault) return currentDefault;
  if (saved === 'true' || saved === 'false') return saved === 'true';
  return currentDefault;
}

/**
 * Helper to get environment-specific localStorage keys
 * Each environment (dev, test, test2, etc.) gets its own token storage
 * This prevents token conflicts when running multiple environments simultaneously
 */
export const getStorageKey = (key: string): string => {
  const basePath = import.meta.env.BASE_URL || '/'
  // Normalize: /test2/ -> test2, / -> root
  const envName = basePath.replace(/^\/|\/$/g, '') || 'root'
  return `${envName}_${key}`
}

export type DeviceType = 'neo' | 'omi' | 'unknown';

export function detectDeviceType(name: string | null): DeviceType {
  const lower = (name || '').toLowerCase();
  if (lower.includes('neo')) return 'neo';
  if (lower.includes('omi') || lower.includes('friend') || lower.includes('elato')) return 'omi';
  return 'unknown';
}

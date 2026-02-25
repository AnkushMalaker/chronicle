export type DeviceType = 'neo' | 'omi' | 'unknown';

export function detectDeviceType(name: string | null): DeviceType {
  const lower = (name || '').toLowerCase();
  if (lower.includes('neo')) return 'neo';
  if (lower.includes('omi') || lower.includes('friend')) return 'omi';
  return 'unknown';
}

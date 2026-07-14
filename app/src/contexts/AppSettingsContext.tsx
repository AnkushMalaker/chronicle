import React, { createContext, useContext } from 'react';
import { useAppSettings, AppSettings } from '@/hooks/useAppSettings';

// Shared app-settings state. useAppSettings holds local state hydrated from
// storage, so each call site would otherwise get its own copy — meaning a login
// or QR scan performed on the Settings screen wouldn't be reflected on the home
// screen until it remounted. Providing a single instance via context keeps both
// screens in sync.
const AppSettingsContext = createContext<AppSettings | null>(null);

export const AppSettingsProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const settings = useAppSettings();
  return <AppSettingsContext.Provider value={settings}>{children}</AppSettingsContext.Provider>;
};

export const useSharedAppSettings = (): AppSettings => {
  const ctx = useContext(AppSettingsContext);
  if (!ctx) {
    throw new Error('useSharedAppSettings must be used within an AppSettingsProvider');
  }
  return ctx;
};

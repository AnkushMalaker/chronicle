/**
 * Theme context — resolves the active theme and exposes it to the tree.
 *
 * The preference is tri-state: `system` (default) follows the OS appearance,
 * while `light`/`dark` pin it. The choice is persisted, so the app reopens the
 * way the user left it.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useColorScheme } from 'react-native';

import { getThemePreference, saveThemePreference } from '@/utils/storage';

import { themes, type Theme, type ThemeName } from './themes';

export type ThemeMode = 'system' | ThemeName;

interface ThemeContextValue {
  /** The resolved theme to style with. */
  theme: Theme;
  /** The stored preference, which may be `system`. */
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

interface ThemeProviderProps {
  children: React.ReactNode;
  /**
   * Pins the theme regardless of preference. Only for rendering a specific
   * theme deliberately, such as the side-by-side style guide.
   */
  forced?: ThemeName;
}

export function ThemeProvider({ children, forced }: ThemeProviderProps) {
  const systemScheme = useColorScheme();
  const [mode, setModeState] = useState<ThemeMode>('system');

  useEffect(() => {
    let active = true;
    getThemePreference().then((stored) => {
      if (active && stored) {
        setModeState(stored);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
    void saveThemePreference(next);
  }, []);

  const value = useMemo<ThemeContextValue>(() => {
    const resolved: ThemeName =
      forced ?? (mode === 'system' ? (systemScheme === 'light' ? 'light' : 'dark') : mode);
    return { theme: themes[resolved], mode, setMode };
  }, [forced, mode, systemScheme, setMode]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/** The active theme. Every colour, spacing, and type value comes from here. */
export function useTheme(): Theme {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return context.theme;
}

/** The light/dark preference and its setter, for the settings toggle. */
export function useThemeMode(): Pick<ThemeContextValue, 'mode' | 'setMode'> {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error('useThemeMode must be used within a ThemeProvider');
  }
  return { mode: context.mode, setMode: context.setMode };
}

import { useColorScheme } from 'react-native';

export interface ThemeColors {
  background: string;
  card: string;
  cardBorder: string;
  text: string;
  textSecondary: string;
  textTertiary: string;
  primary: string;
  success: string;
  warning: string;
  danger: string;
  inputBackground: string;
  inputBorder: string;
  separator: string;
  disabled: string;
}

const lightColors: ThemeColors = {
  background: '#f5f5f5',
  card: '#ffffff',
  cardBorder: '#e0e0e0',
  text: '#333333',
  textSecondary: '#555555',
  textTertiary: '#888888',
  primary: '#007AFF',
  success: '#34C759',
  warning: '#FF9500',
  danger: '#FF3B30',
  inputBackground: '#f0f0f0',
  inputBorder: '#dddddd',
  separator: '#e0e0e0',
  disabled: '#A0A0A0',
};

const darkColors: ThemeColors = {
  background: '#000000',
  card: '#1c1c1e',
  cardBorder: '#38383a',
  text: '#f2f2f7',
  textSecondary: '#aeaeb2',
  textTertiary: '#636366',
  primary: '#0a84ff',
  success: '#30d158',
  warning: '#ff9f0a',
  danger: '#ff453a',
  inputBackground: '#2c2c2e',
  inputBorder: '#38383a',
  separator: '#38383a',
  disabled: '#636366',
};

export function useTheme() {
  const scheme = useColorScheme();
  const isDark = scheme === 'dark';
  return { colors: isDark ? darkColors : lightColors, isDark };
}

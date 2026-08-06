/**
 * Chronicle app theme.
 *
 *   import { useTheme } from '@/theme';
 *
 *   const t = useTheme();
 *   const s = createStyles(t);
 *   const createStyles = (t: Theme) => StyleSheet.create({
 *     card: { backgroundColor: t.color.surface.raised, padding: t.space[4] },
 *   });
 *
 * To reskin the app, edit `palette.ts`. To change what a colour *means*, edit
 * `themes.ts`. Components should never contain a colour literal.
 */

export { ThemeProvider, useTheme, useThemeMode, type ThemeMode } from './ThemeProvider';
export {
  darkTheme,
  lightTheme,
  themes,
  type SoftColor,
  type StatusColor,
  type Theme,
  type ThemeColor,
  type ThemeName,
  type ThemeShadow,
} from './themes';
export { hitTarget } from './tokens';

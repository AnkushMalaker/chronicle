/**
 * Chronicle non-colour design tokens — spacing, radii, type, elevation, motion.
 *
 * Ported from the Chronicle Design System (`design-system/tokens/*.css`) and
 * translated to React Native units: CSS `px` become unitless RN density points,
 * and CSS `box-shadow` becomes the RN `shadow*` + Android `elevation` pair.
 *
 * These are theme-independent (they do not change between light and dark); only
 * `shadow` is built per-theme, in `themes.ts`.
 */

import { Platform, type TextStyle } from 'react-native';

/** 4px base grid, matching the design system's spacing scale. */
export const space = {
  px: 1,
  0.5: 2,
  1: 4,
  1.5: 6,
  2: 8,
  3: 12,
  4: 16,
  5: 20,
  6: 24,
  8: 32,
  10: 40,
  12: 48,
} as const;

/** Corner radii. `full` is the pill/circle radius. */
export const radius = {
  sm: 4, // chips, small tags
  md: 6, // buttons, list rows
  lg: 8, // cards, inputs, tabs
  xl: 12, // large panels
  full: 9999,
} as const;

export const borderWidth = 1;

/**
 * Font families. RN has no `system-ui`, so each platform names its own default
 * face; on web we can pass the CSS stack straight through.
 */
export const fontFamily = {
  sans: Platform.select({
    ios: 'System',
    android: 'sans-serif',
    default: 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  }) as string,
  mono: Platform.select({
    ios: 'Menlo',
    android: 'monospace',
    default: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
  }) as string,
} as const;

/**
 * Type scale — size paired with its line height, so callers never have to
 * remember the pairing. Spread one into a style: `...t.type.sm`.
 */
export const type = {
  xs: { fontSize: 12, lineHeight: 16 },
  sm: { fontSize: 14, lineHeight: 20 },
  base: { fontSize: 16, lineHeight: 24 },
  lg: { fontSize: 18, lineHeight: 28 },
  xl: { fontSize: 20, lineHeight: 28 },
  '2xl': { fontSize: 24, lineHeight: 32 },
  '3xl': { fontSize: 30, lineHeight: 36 },
} as const;

/** Font weights, as the string literals RN's `TextStyle` expects. */
export const weight = {
  regular: '400',
  medium: '500',
  semibold: '600',
  bold: '700',
} as const satisfies Record<string, TextStyle['fontWeight']>;

/** Letter spacing for uppercase eyebrows / group labels. */
export const tracking = {
  tight: -0.2,
  normal: 0,
  wide: 1.0,
} as const;

/** Animation timings, for `Animated` and `LayoutAnimation`. */
export const motion = {
  fast: 120,
  base: 200,
} as const;

/** Minimum touch target, per the platform accessibility guidelines. */
export const hitTarget = 44;

export type Space = typeof space;
export type Radius = typeof radius;
export type TypeScale = typeof type;

/**
 * Chronicle "Espresso" palette — the raw colour ramps.
 *
 * THIS IS THE ONE FILE TO EDIT TO RESKIN THE APP. Nothing outside `src/theme/`
 * may contain a colour literal; every component reads semantic tokens from
 * `useTheme()`, and those tokens are assembled from the ramps below in
 * `themes.ts`. Swap the hex values here (or repoint a semantic alias there) and
 * the whole app follows.
 *
 * Source of truth: the Chronicle Design System project (claude.ai/design),
 * `tokens/colors.css` — the same palette the two web UIs get through
 * `chronicle-espresso-preset.js`. Warm espresso neutrals, a terracotta brand
 * ramp, and a forest-green family (greens double as the positive/verifier
 * status, so they belong to the palette). Dark is the canonical theme.
 *
 * Ramps run light (50) → dark (950) so a light/dark pair is always a swap of
 * two ends of the same ramp.
 */

/** Warm espresso neutrals — every surface, border, and text colour. */
export const espresso = {
  50: '#f7f3ea',
  100: '#f2ece2',
  200: '#ddd5c6',
  300: '#c9bfae',
  400: '#948976',
  500: '#6b5f4f',
  600: '#42392f',
  700: '#2c251d',
  800: '#211b15',
  900: '#191410',
  950: '#120d0a',
} as const;

/** Brand terracotta — primary actions, active nav/tabs, focus rings. */
export const terracotta = {
  50: '#fbeee7',
  100: '#f7dccd',
  200: '#f0c3ac',
  300: '#ecab93',
  400: '#e07856',
  500: '#d2694a',
  600: '#c2551f',
  700: '#a8471f',
  800: '#7c351a',
  900: '#3a1f14',
  950: '#241009',
} as const;

/** Forest green — success / connected / verified. */
export const forest = {
  50: '#eaf3ec',
  100: '#d1e7d5',
  200: '#a9cfb0',
  300: '#8fc79a',
  400: '#6f9a5f',
  500: '#4f7d54',
  600: '#3f6b47',
  700: '#34614a',
  800: '#294a39',
  900: '#1f3729',
  950: '#132218',
} as const;

/** Danger red — destructive actions, errors, disconnected states. */
export const danger = {
  50: '#fbeae7',
  100: '#f7d5cf',
  200: '#f0b3a8',
  300: '#eda093',
  400: '#e8735f',
  500: '#dc4a3a',
  600: '#c53a2b',
  700: '#a32d20',
  800: '#7d2419',
  900: '#5a1a12',
  950: '#360e09',
} as const;

/** Warning amber — scanning, degraded, needs-attention. */
export const amber = {
  50: '#fcf4e1',
  100: '#f8e7bb',
  200: '#f2d488',
  300: '#f0c674',
  400: '#e6ad3f',
  500: '#d99521',
  600: '#b8781a',
  700: '#925c15',
  800: '#6d4513',
  900: '#4a2f0f',
  950: '#2b1b08',
} as const;

/** Suggest purple — advisory / AI-suggested content. */
export const purple = {
  50: '#f4eef8',
  100: '#e7d9ef',
  200: '#d4bce2',
  300: '#c2a0d4',
  400: '#a986c4',
  500: '#9169b0',
  600: '#775394',
  700: '#5f4278',
  800: '#48335c',
  900: '#312340',
  950: '#1d1526',
} as const;

/** Muted info blue — neutral informational notes, kept distinct from the brand. */
export const info = {
  50: '#eef3f7',
  100: '#d7e3ec',
  200: '#b5cbdd',
  300: '#8fb0c9',
  400: '#6b93b0',
  500: '#4f7896',
  600: '#3f6079',
  700: '#344e61',
  800: '#2a3d4b',
  900: '#1f2c37',
  950: '#141d24',
} as const;

/**
 * Light-theme surfaces. These are a designed warm-cream set rather than an
 * inversion of the espresso ramp — a flat `#fff` page reads cold and breaks the
 * family, so the design system specifies its own values.
 */
export const cream = {
  page: '#f4efe6',
  raised: '#fffdf8',
  sunken: '#ece5d8',
  border: '#ddd3c2',
  borderSubtle: '#ebe4d6',
  textPrimary: '#2a2018',
  textSecondary: '#5a4e40',
  textMuted: '#8a7d68',
  textFaint: '#a89a83',
} as const;

/**
 * Light-theme accent and status foregrounds. The dark theme's mid-ramp values
 * are too light to hit AA on cream, so the design system darkens them; these
 * are those tuned values.
 */
export const lightInk = {
  accent: '#b0491c', // deep terracotta — white text passes AA
  accentHover: '#953c15',
  accentFg: '#a8471f',
  accentNavBg: '#f6e2d5',
  successFg: '#3f6b47',
  successSoftBg: '#dceadc',
  dangerFg: '#b5432f',
  dangerSoftBg: '#f7e2da',
  warningFg: '#9a6a12',
  warningSoftBg: '#f7eccf',
  infoFg: '#3a6a8f',
  infoSoftBg: '#dde8f0',
  suggestFg: '#7a5a8c',
  suggestSoftBg: '#efe4f2',
} as const;

/**
 * Absolutes. `shadowBase` is warm-tinted rather than neutral black so elevation
 * sits in the same family as the espresso surfaces.
 */
export const shadowBase = '#140c06';
export const white = '#ffffff';

/**
 * Adds an alpha channel to a `#rrggbb` literal. Used to build the translucent
 * "soft" status chip fills from the ramps, so a chip stays in the same hue
 * family as its solid counterpart.
 */
export function withAlpha(hex: string, alpha: number): string {
  const value = hex.replace('#', '');
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

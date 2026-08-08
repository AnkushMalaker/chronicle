/**
 * Semantic theme definitions — where the raw ramps in `palette.ts` become
 * meaning ("the page background", "the danger foreground").
 *
 * Components must only ever read from here (via `useTheme()`). They should not
 * import `palette.ts` directly: naming a ramp step in a component is what makes
 * a palette impossible to change later.
 *
 * Mirrors the semantic aliases in the Chronicle Design System's
 * `tokens/colors.css`, so the app and the two web UIs stay in step.
 */

import {
  amber,
  cream,
  danger,
  espresso,
  forest,
  info,
  lightInk,
  purple,
  shadowBase,
  terracotta,
  white,
  withAlpha,
} from './palette';
import { borderWidth, fontFamily, motion, radius, space, tracking, type, weight } from './tokens';

import type { ViewStyle } from 'react-native';

export type ThemeName = 'light' | 'dark';

/** A status hue with a solid fill, a readable foreground, and a soft chip fill. */
export interface StatusColor {
  /** Solid fill — status dots, filled buttons, progress. */
  base: string;
  /** Pressed state of `base`. */
  pressed: string;
  /** Text/icon colour for this status on a page or card surface. */
  fg: string;
  /** Translucent fill for chips and inline banners. */
  softBg: string;
  /** Text/icon colour when sitting ON the solid `base` fill. */
  on: string;
}

/** An advisory hue that only ever appears as a chip — no solid fill. */
export interface SoftColor {
  fg: string;
  softBg: string;
}

export interface ThemeColor {
  surface: {
    /** App background. */
    page: string;
    /** Cards, headers, sheets — one step up from the page. */
    raised: string;
    /** Inputs, inner tiles, code blocks — one step down from a card. */
    sunken: string;
  };
  border: {
    /** Default hairline. */
    base: string;
    /** Quieter divider inside an already-bordered container. */
    subtle: string;
  };
  text: {
    primary: string;
    secondary: string;
    muted: string;
    faint: string;
  };
  accent: {
    /** Primary action fill, active tab/nav. */
    base: string;
    /** Pressed/hover state of `base`. */
    hover: string;
    /** Text/icons ON the accent fill. */
    on: string;
    /** Accent-coloured text/icons on a page or card surface. */
    fg: string;
    /** Tinted background for an active nav row. */
    navBg: string;
  };
  status: {
    success: StatusColor;
    danger: StatusColor;
    warning: StatusColor;
    info: SoftColor;
    suggest: SoftColor;
  };
  chip: {
    bg: string;
    /** Pressed state of `bg`, for chips used as secondary buttons. */
    bgPressed: string;
    fg: string;
  };
  /** Scrim behind modals. */
  overlay: string;
  /** Disabled control fill. */
  disabled: string;
}

export interface ThemeShadow {
  sm: ViewStyle;
  lg: ViewStyle;
}

export interface Theme {
  name: ThemeName;
  isDark: boolean;
  color: ThemeColor;
  space: typeof space;
  radius: typeof radius;
  borderWidth: typeof borderWidth;
  font: typeof fontFamily;
  type: typeof type;
  weight: typeof weight;
  tracking: typeof tracking;
  motion: typeof motion;
  shadow: ThemeShadow;
}

/**
 * Elevation. RN needs `elevation` for Android and the `shadow*` quartet for
 * iOS/web; the shadow is deliberately stronger on dark, where a warm ambient
 * shadow is the only thing separating two near-black surfaces.
 */
const makeShadow = (isDark: boolean): ThemeShadow => ({
  sm: {
    shadowColor: shadowBase,
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: isDark ? 0.4 : 0.12,
    shadowRadius: 2,
    elevation: 2,
  },
  lg: {
    shadowColor: shadowBase,
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: isDark ? 0.55 : 0.18,
    shadowRadius: 16,
    elevation: 8,
  },
});

const darkColor: ThemeColor = {
  surface: {
    page: espresso[800],
    raised: espresso[700],
    sunken: espresso[900],
  },
  border: {
    base: espresso[600],
    subtle: espresso[700],
  },
  text: {
    primary: espresso[100],
    secondary: espresso[300],
    muted: espresso[400],
    faint: espresso[500],
  },
  accent: {
    base: terracotta[500],
    hover: terracotta[600],
    on: terracotta[950],
    fg: terracotta[300],
    navBg: terracotta[900],
  },
  status: {
    success: {
      base: forest[500],
      pressed: forest[600],
      fg: forest[300],
      softBg: withAlpha(forest[700], 0.4),
      on: white,
    },
    danger: {
      base: danger[600],
      pressed: danger[700],
      fg: danger[300],
      softBg: withAlpha(danger[800], 0.42),
      on: white,
    },
    warning: {
      base: amber[500],
      pressed: amber[600],
      fg: amber[300],
      softBg: withAlpha(amber[800], 0.45),
      // Amber is too light for white text; ink it with the darkest amber.
      on: amber[950],
    },
    info: {
      fg: info[300],
      softBg: withAlpha(info[800], 0.45),
    },
    suggest: {
      fg: purple[300],
      softBg: withAlpha(purple[800], 0.45),
    },
  },
  chip: {
    bg: espresso[600],
    bgPressed: espresso[500],
    fg: espresso[300],
  },
  overlay: withAlpha(espresso[950], 0.7),
  disabled: espresso[600],
};

const lightColor: ThemeColor = {
  surface: {
    page: cream.page,
    raised: cream.raised,
    sunken: cream.sunken,
  },
  border: {
    base: cream.border,
    subtle: cream.borderSubtle,
  },
  text: {
    primary: cream.textPrimary,
    secondary: cream.textSecondary,
    muted: cream.textMuted,
    faint: cream.textFaint,
  },
  accent: {
    base: lightInk.accent,
    hover: lightInk.accentHover,
    on: white,
    fg: lightInk.accentFg,
    navBg: lightInk.accentNavBg,
  },
  status: {
    success: {
      base: forest[600],
      pressed: forest[700],
      fg: lightInk.successFg,
      softBg: lightInk.successSoftBg,
      on: white,
    },
    danger: {
      base: danger[600],
      pressed: danger[700],
      fg: lightInk.dangerFg,
      softBg: lightInk.dangerSoftBg,
      on: white,
    },
    warning: {
      base: amber[600],
      pressed: amber[700],
      fg: lightInk.warningFg,
      softBg: lightInk.warningSoftBg,
      on: white,
    },
    info: {
      fg: lightInk.infoFg,
      softBg: lightInk.infoSoftBg,
    },
    suggest: {
      fg: lightInk.suggestFg,
      softBg: lightInk.suggestSoftBg,
    },
  },
  chip: {
    bg: cream.sunken,
    bgPressed: cream.border,
    fg: cream.textSecondary,
  },
  overlay: withAlpha(espresso[900], 0.45),
  disabled: cream.border,
};

const base = {
  space,
  radius,
  borderWidth,
  font: fontFamily,
  type,
  weight,
  tracking,
  motion,
} as const;

export const darkTheme: Theme = {
  name: 'dark',
  isDark: true,
  color: darkColor,
  shadow: makeShadow(true),
  ...base,
};

export const lightTheme: Theme = {
  name: 'light',
  isDark: false,
  color: lightColor,
  shadow: makeShadow(false),
  ...base,
};

export const themes: Record<ThemeName, Theme> = {
  dark: darkTheme,
  light: lightTheme,
};

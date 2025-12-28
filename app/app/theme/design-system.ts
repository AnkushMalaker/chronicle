/**
 * Chronicle Mobile App - Design System
 *
 * Dark mode theme with emerald green & violet purple accents.
 * Brand colors inspired by Ushadow/Chronicle identity.
 */

export const theme = {
  // App branding
  app: {
    name: 'Chronicle',
    tagline: 'Your AI Memory',
  },

  // Color Palette - Dark mode with green/purple accents
  colors: {
    // Primary brand colors - Emerald green family
    primary: {
      main: '#10B981',      // Emerald green - vibrant, modern
      light: '#34D399',     // Light emerald
      dark: '#059669',      // Dark emerald
      contrast: '#052E16',  // Dark green text for WCAG AA (8.59:1 contrast)
    },

    // Secondary accent colors - Orchid purple family
    secondary: {
      main: '#A855F7',      // Violet/purple - complementary accent
      light: '#C084FC',     // Light violet
      dark: '#7C3AED',      // Dark violet
      contrast: '#000000',  // Black text for WCAG AA (7.25:1 contrast)
    },

    // Semantic colors
    success: {
      main: '#22C55E',      // Green (harmonizes with primary)
      light: '#4ADE80',
      dark: '#16A34A',
      background: '#052E16',  // Dark green bg
      contrast: '#052E16',    // Dark text for WCAG AA
    },

    warning: {
      main: '#F59E0B',      // Amber
      light: '#FBBF24',
      dark: '#D97706',
      background: '#422006',  // Dark amber bg
      contrast: '#422006',    // Dark amber text for WCAG AA
    },

    error: {
      main: '#EF4444',      // Red
      light: '#F87171',
      dark: '#DC2626',
      background: '#450A0A',  // Dark red bg
      contrast: '#000000',    // Black text for WCAG AA (5.41:1 contrast)
    },

    // Neutral grays - Dark mode palette
    gray: {
      50: '#18181B',        // Darkest (zinc-900)
      100: '#27272A',       // Very dark (zinc-800)
      200: '#3F3F46',       // Dark (zinc-700)
      300: '#52525B',       // Medium dark (zinc-600)
      400: '#71717A',       // Medium (zinc-500)
      500: '#A1A1AA',       // Light medium (zinc-400)
      600: '#D4D4D8',       // Light (zinc-300)
      700: '#E4E4E7',       // Very light (zinc-200)
      800: '#F4F4F5',       // Near white (zinc-100)
      900: '#FAFAFA',       // White-ish (zinc-50)
    },

    // Background colors - Dark mode
    background: {
      primary: '#09090B',     // Near black (zinc-950)
      secondary: '#18181B',   // Very dark (zinc-900)
      tertiary: '#27272A',    // Dark (zinc-800)
      elevated: '#27272A',    // For cards/modals
    },

    // Text colors - Dark mode
    text: {
      primary: '#FAFAFA',     // White-ish (19:1 contrast)
      secondary: '#A1A1AA',   // Muted gray (7.76:1 contrast)
      tertiary: '#9CA3AF',    // Lighter muted (6.3:1 contrast - WCAG AA)
      disabled: '#9CA3AF',    // Same as tertiary for visibility
      inverse: '#000000',     // Black text on light bg (maximum contrast)
    },

    // Border colors - Dark mode
    border: {
      light: '#27272A',       // Subtle
      medium: '#3F3F46',      // Medium visibility
      dark: '#52525B',        // High visibility
    },

    // Connection status colors
    status: {
      healthy: '#22C55E',
      checking: '#F59E0B',
      unhealthy: '#EF4444',
      unknown: '#71717A',
    },
  },

  // Spacing scale (base 4px)
  spacing: {
    xs: 4,
    sm: 8,
    md: 16,
    lg: 24,
    xl: 32,
    xxl: 48,
  },

  // Border radius
  borderRadius: {
    sm: 8,
    md: 12,
    lg: 16,
    xl: 20,
    full: 9999,
  },

  // Typography
  typography: {
    // Font families
    fontFamily: {
      regular: 'System',
      medium: 'System',
      semibold: 'System',
      bold: 'System',
    },

    // Font sizes
    fontSize: {
      xs: 12,
      sm: 14,
      md: 16,
      lg: 18,
      xl: 20,
      xxl: 24,
      xxxl: 32,
    },

    // Font weights
    fontWeight: {
      regular: '400' as const,
      medium: '500' as const,
      semibold: '600' as const,
      bold: '700' as const,
    },

    // Line heights
    lineHeight: {
      tight: 1.2,
      normal: 1.5,
      relaxed: 1.75,
    },
  },

  // Shadows (subtle for dark mode)
  shadows: {
    none: {
      shadowColor: 'transparent',
      shadowOffset: { width: 0, height: 0 },
      shadowOpacity: 0,
      shadowRadius: 0,
      elevation: 0,
    },
    sm: {
      shadowColor: '#000',
      shadowOffset: { width: 0, height: 2 },
      shadowOpacity: 0.3,
      shadowRadius: 4,
      elevation: 2,
    },
    md: {
      shadowColor: '#000',
      shadowOffset: { width: 0, height: 4 },
      shadowOpacity: 0.4,
      shadowRadius: 8,
      elevation: 4,
    },
    lg: {
      shadowColor: '#000',
      shadowOffset: { width: 0, height: 8 },
      shadowOpacity: 0.5,
      shadowRadius: 16,
      elevation: 8,
    },
    // Glow effect for primary color
    glow: {
      shadowColor: '#10B981',
      shadowOffset: { width: 0, height: 0 },
      shadowOpacity: 0.4,
      shadowRadius: 12,
      elevation: 6,
    },
  },

  // Component-specific styles - Dark mode
  components: {
    card: {
      backgroundColor: '#18181B',  // zinc-900
      borderRadius: 12,
      padding: 16,
      borderWidth: 1,
      borderColor: '#27272A',
      ...{
        shadowColor: '#000',
        shadowOffset: { width: 0, height: 2 },
        shadowOpacity: 0.3,
        shadowRadius: 4,
        elevation: 2,
      },
    },

    button: {
      primary: {
        backgroundColor: '#10B981',  // Emerald green
        borderRadius: 12,
        paddingVertical: 14,
        paddingHorizontal: 24,
      },
      secondary: {
        backgroundColor: '#27272A',  // Dark gray
        borderRadius: 12,
        paddingVertical: 14,
        paddingHorizontal: 24,
      },
      accent: {
        backgroundColor: '#A855F7',  // Violet accent
        borderRadius: 12,
        paddingVertical: 14,
        paddingHorizontal: 24,
      },
      danger: {
        backgroundColor: '#EF4444',  // Red
        borderRadius: 12,
        paddingVertical: 14,
        paddingHorizontal: 24,
      },
    },

    input: {
      backgroundColor: '#18181B',  // zinc-900
      borderWidth: 1,
      borderColor: '#3F3F46',      // zinc-700
      borderRadius: 12,
      paddingVertical: 12,
      paddingHorizontal: 16,
      fontSize: 16,
      color: '#FAFAFA',            // White text
    },
  },
};

// Helper function to get spacing value
export const getSpacing = (...values: number[]): number => {
  return values.reduce((acc, val) => acc + theme.spacing.md * val, 0);
};

// Helper function for responsive spacing
export const spacing = (multiplier: number): number => {
  return theme.spacing.md * multiplier;
};

export default theme;

/**
 * Button — the Chronicle design system's button, in React Native.
 *
 * One `primary` per view; `secondary` for neutral actions; `outline` for a
 * quiet alternative that still reads as a button; `danger` to destroy; `ghost`
 * for the quietest actions. Mirrors `design-system/components/core/Button.jsx`.
 */

import React from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type ViewStyle,
} from 'react-native';

import { useTheme, type Theme } from '@/theme';

export type ButtonVariant =
  | 'primary'
  | 'secondary'
  | 'outline'
  | 'danger'
  | 'warning'
  | 'ghost'
  | 'link';
export type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps {
  children: React.ReactNode;
  onPress?: () => void;
  variant?: ButtonVariant;
  size?: ButtonSize;
  disabled?: boolean;
  /** Shows a spinner and blocks presses. */
  loading?: boolean;
  /** Stretches to the container width — the norm for a form's submit action. */
  fullWidth?: boolean;
  /** Rendered before the label. */
  icon?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  accessibilityLabel?: string;
}

/** Background, label, and border for each variant, in both press states. */
function variantColors(t: Theme, variant: ButtonVariant) {
  switch (variant) {
    case 'primary':
      return {
        bg: t.color.accent.base,
        bgPressed: t.color.accent.hover,
        fg: t.color.accent.on,
        border: 'transparent',
      };
    case 'danger':
      return {
        bg: t.color.status.danger.base,
        bgPressed: t.color.status.danger.pressed,
        fg: t.color.status.danger.on,
        border: 'transparent',
      };
    case 'warning':
      // In-progress / stop-this-running-thing. Amber is too light for white,
      // so the label is inked with the darkest amber.
      return {
        bg: t.color.status.warning.base,
        bgPressed: t.color.status.warning.pressed,
        fg: t.color.status.warning.on,
        border: 'transparent',
      };
    case 'outline':
      return {
        bg: 'transparent',
        bgPressed: t.color.accent.navBg,
        fg: t.color.accent.fg,
        border: t.color.accent.base,
      };
    case 'ghost':
      return {
        bg: 'transparent',
        bgPressed: t.color.surface.sunken,
        fg: t.color.text.secondary,
        border: 'transparent',
      };
    case 'link':
      // Chrome-less but accent-tinted — a text action that still reads as the
      // accent colour, e.g. an inline "Refresh".
      return {
        bg: 'transparent',
        bgPressed: t.color.surface.sunken,
        fg: t.color.accent.fg,
        border: 'transparent',
      };
    case 'secondary':
    default:
      return {
        bg: t.color.chip.bg,
        bgPressed: t.color.chip.bgPressed,
        fg: t.color.chip.fg,
        border: 'transparent',
      };
  }
}

const SIZES = {
  sm: { paddingVertical: 6, paddingHorizontal: 12, minHeight: 32, text: 'xs' },
  md: { paddingVertical: 10, paddingHorizontal: 16, minHeight: 44, text: 'sm' },
  lg: { paddingVertical: 12, paddingHorizontal: 20, minHeight: 48, text: 'base' },
} as const;

export function Button({
  children,
  onPress,
  variant = 'secondary',
  size = 'md',
  disabled = false,
  loading = false,
  fullWidth = false,
  icon,
  style,
  accessibilityLabel,
}: ButtonProps) {
  const t = useTheme();
  const s = createStyles(t);
  const colors = variantColors(t, variant);
  const dimensions = SIZES[size];
  const isInactive = disabled || loading;

  return (
    <Pressable
      onPress={onPress}
      disabled={isInactive}
      accessibilityRole="button"
      accessibilityState={{ disabled: isInactive, busy: loading }}
      accessibilityLabel={accessibilityLabel}
      style={({ pressed }) => [
        s.base,
        {
          paddingVertical: dimensions.paddingVertical,
          paddingHorizontal: dimensions.paddingHorizontal,
          minHeight: dimensions.minHeight,
          backgroundColor: pressed && !isInactive ? colors.bgPressed : colors.bg,
          borderColor: colors.border,
        },
        fullWidth && s.fullWidth,
        // The design system dims a disabled button rather than recolouring it,
        // so it keeps its identity while reading as unavailable.
        isInactive && s.inactive,
        style,
      ]}
    >
      {loading ? <ActivityIndicator size="small" color={colors.fg} /> : icon}
      {children != null && (
        <Text
          style={[
            s.label,
            t.type[dimensions.text],
            { color: colors.fg },
          ]}
          numberOfLines={1}
        >
          {children}
        </Text>
      )}
    </Pressable>
  );
}

/** Horizontal group of buttons with consistent spacing. */
export function ButtonRow({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  const t = useTheme();
  return <View style={[{ flexDirection: 'row', gap: t.space[2] }, style]}>{children}</View>;
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    base: {
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'center',
      gap: t.space[1.5],
      borderRadius: t.radius.lg,
      borderWidth: t.borderWidth,
    },
    fullWidth: {
      alignSelf: 'stretch',
    },
    inactive: {
      opacity: 0.4,
    },
    label: {
      fontFamily: t.font.sans,
      fontWeight: t.weight.semibold,
    },
  });

/**
 * Badge and StatusDot — the design system's status vocabulary.
 *
 * A `Badge` is a soft-filled chip carrying a short state word; a `StatusDot` is
 * the same state reduced to a dot for dense rows. Both take the same `tone`, so
 * a connected device reads green in either form. Mirrors
 * `design-system/components/core/Badge.jsx`.
 */

import React from 'react';
import { StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';

import { useTheme, type Theme } from '@/theme';

export type Tone = 'neutral' | 'success' | 'danger' | 'warning' | 'info' | 'suggest' | 'accent';

/** Soft chip fill + foreground for a tone. */
function toneColors(t: Theme, tone: Tone): { bg: string; fg: string } {
  switch (tone) {
    case 'success':
      return { bg: t.color.status.success.softBg, fg: t.color.status.success.fg };
    case 'danger':
      return { bg: t.color.status.danger.softBg, fg: t.color.status.danger.fg };
    case 'warning':
      return { bg: t.color.status.warning.softBg, fg: t.color.status.warning.fg };
    case 'info':
      return { bg: t.color.status.info.softBg, fg: t.color.status.info.fg };
    case 'suggest':
      return { bg: t.color.status.suggest.softBg, fg: t.color.status.suggest.fg };
    case 'accent':
      return { bg: t.color.accent.navBg, fg: t.color.accent.fg };
    case 'neutral':
    default:
      return { bg: t.color.chip.bg, fg: t.color.chip.fg };
  }
}

/** The solid dot colour for a tone. */
export function toneDotColor(t: Theme, tone: Tone): string {
  switch (tone) {
    case 'success':
      return t.color.status.success.base;
    case 'danger':
      return t.color.status.danger.base;
    case 'warning':
      return t.color.status.warning.base;
    case 'info':
      return t.color.status.info.fg;
    case 'suggest':
      return t.color.status.suggest.fg;
    case 'accent':
      return t.color.accent.base;
    case 'neutral':
    default:
      return t.color.disabled;
  }
}

interface BadgeProps {
  children: React.ReactNode;
  tone?: Tone;
  /** Uses the monospace face, for IDs, versions, and measurements. */
  mono?: boolean;
  style?: StyleProp<ViewStyle>;
}

export function Badge({ children, tone = 'neutral', mono = false, style }: BadgeProps) {
  const t = useTheme();
  const s = createStyles(t);
  const colors = toneColors(t, tone);

  return (
    <View style={[s.badge, { backgroundColor: colors.bg }, style]}>
      <Text style={[s.label, { color: colors.fg }, mono && s.mono]} numberOfLines={1}>
        {children}
      </Text>
    </View>
  );
}

interface StatusDotProps {
  tone?: Tone;
  size?: number;
  style?: StyleProp<ViewStyle>;
}

export function StatusDot({ tone = 'neutral', size = 10, style }: StatusDotProps) {
  const t = useTheme();
  return (
    <View
      accessibilityElementsHidden
      importantForAccessibility="no"
      style={[
        {
          width: size,
          height: size,
          borderRadius: size / 2,
          backgroundColor: toneDotColor(t, tone),
        },
        style,
      ]}
    />
  );
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    badge: {
      alignSelf: 'flex-start',
      paddingHorizontal: t.space[2],
      paddingVertical: t.space[0.5],
      borderRadius: t.radius.sm,
    },
    label: {
      fontFamily: t.font.sans,
      ...t.type.xs,
      fontWeight: t.weight.medium,
    },
    mono: {
      fontFamily: t.font.mono,
    },
  });

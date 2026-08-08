/**
 * Card — a raised surface grouping one topic, optionally with a title.
 *
 * The app's screens are stacks of these. Mirrors
 * `design-system/components/core/Card.jsx`, plus the title row that the mobile
 * screens repeat on every section.
 */

import React from 'react';
import { StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';

import { useTheme, type Theme } from '@/theme';

interface CardProps {
  children: React.ReactNode;
  /** Rendered as the card's heading. */
  title?: string;
  /** Rendered on the title row, right-aligned — a badge or small action. */
  headerRight?: React.ReactNode;
  /** Drops the inner padding, for cards that hold their own edge-to-edge rows. */
  flush?: boolean;
  style?: StyleProp<ViewStyle>;
}

export function Card({ children, title, headerRight, flush = false, style }: CardProps) {
  const t = useTheme();
  const s = createStyles(t);

  return (
    <View style={[s.card, flush && s.flush, style]}>
      {(title || headerRight) && (
        <View style={[s.header, flush && s.flushHeader]}>
          {title ? <Text style={s.title}>{title}</Text> : <View />}
          {headerRight}
        </View>
      )}
      {children}
    </View>
  );
}

/**
 * A sunken tile inside a card — the inner well used for status readouts and
 * grouped detail rows.
 */
export function CardWell({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  const t = useTheme();
  const s = createStyles(t);
  return <View style={[s.well, style]}>{children}</View>;
}

/** Hairline divider between rows inside a card. */
export function Divider({ style }: { style?: StyleProp<ViewStyle> }) {
  const t = useTheme();
  const s = createStyles(t);
  return <View style={[s.divider, style]} />;
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    card: {
      backgroundColor: t.color.surface.raised,
      borderRadius: t.radius.lg,
      borderWidth: t.borderWidth,
      borderColor: t.color.border.base,
      padding: t.space[4],
      marginBottom: t.space[4],
      ...t.shadow.sm,
    },
    flush: {
      padding: 0,
      overflow: 'hidden',
    },
    header: {
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: t.space[2],
      marginBottom: t.space[3],
    },
    flushHeader: {
      paddingHorizontal: t.space[4],
      paddingTop: t.space[4],
    },
    title: {
      flex: 1,
      fontFamily: t.font.sans,
      ...t.type.lg,
      fontWeight: t.weight.semibold,
      color: t.color.text.primary,
    },
    well: {
      backgroundColor: t.color.surface.sunken,
      borderRadius: t.radius.md,
      borderWidth: t.borderWidth,
      borderColor: t.color.border.subtle,
      padding: t.space[3],
    },
    divider: {
      height: t.borderWidth,
      backgroundColor: t.color.border.subtle,
    },
  });

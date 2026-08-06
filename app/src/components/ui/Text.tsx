/**
 * Typographic primitives.
 *
 * These exist so a screen never hand-assembles a font size, weight, and colour
 * that is really one of five recurring roles. Anything genuinely one-off should
 * still read its values from `useTheme()` rather than adding a variant here.
 */

import React from 'react';
import { StyleSheet, Text as RNText, type StyleProp, type TextProps, type TextStyle } from 'react-native';

import { useTheme, type Theme } from '@/theme';

interface TypographyProps extends TextProps {
  children: React.ReactNode;
  style?: StyleProp<TextStyle>;
}

/** Screen or card heading. */
export function Heading({ children, style, ...rest }: TypographyProps) {
  const s = createStyles(useTheme());
  return (
    <RNText style={[s.heading, style]} {...rest}>
      {children}
    </RNText>
  );
}

/** Default running text. */
export function Body({ children, style, ...rest }: TypographyProps) {
  const s = createStyles(useTheme());
  return (
    <RNText style={[s.body, style]} {...rest}>
      {children}
    </RNText>
  );
}

/** Quiet supporting text — hints, timestamps, help copy. */
export function Caption({ children, style, ...rest }: TypographyProps) {
  const s = createStyles(useTheme());
  return (
    <RNText style={[s.caption, style]} {...rest}>
      {children}
    </RNText>
  );
}

/** Uppercase eyebrow that groups the cards beneath it. */
export function SectionLabel({ children, style, ...rest }: TypographyProps) {
  const s = createStyles(useTheme());
  return (
    <RNText style={[s.sectionLabel, style]} {...rest}>
      {children}
    </RNText>
  );
}

/** Monospace, for IDs, URLs, versions, and measurements. */
export function Mono({ children, style, ...rest }: TypographyProps) {
  const s = createStyles(useTheme());
  return (
    <RNText style={[s.mono, style]} {...rest}>
      {children}
    </RNText>
  );
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    heading: {
      fontFamily: t.font.sans,
      ...t.type.lg,
      fontWeight: t.weight.semibold,
      color: t.color.text.primary,
    },
    body: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      color: t.color.text.secondary,
    },
    caption: {
      fontFamily: t.font.sans,
      ...t.type.xs,
      color: t.color.text.muted,
    },
    sectionLabel: {
      fontFamily: t.font.sans,
      ...t.type.xs,
      fontWeight: t.weight.bold,
      color: t.color.text.muted,
      textTransform: 'uppercase',
      letterSpacing: t.tracking.wide,
      marginBottom: t.space[2],
    },
    mono: {
      fontFamily: t.font.mono,
      ...t.type.xs,
      color: t.color.text.muted,
    },
  });

/**
 * InlineAlert — a soft-filled inline banner.
 *
 * Named to avoid colliding with React Native's imperative `Alert` dialog, which
 * these screens also use. Mirrors `design-system/components/feedback/Alert.jsx`.
 */

import React from 'react';
import { StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';

import { useTheme, type Theme } from '@/theme';

import type { Tone } from './Badge';

interface InlineAlertProps {
  children: React.ReactNode;
  tone?: Tone;
  title?: string;
  /** A trailing action, such as a retry button. */
  action?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
}

function alertColors(t: Theme, tone: Tone): { bg: string; fg: string } {
  switch (tone) {
    case 'success':
      return { bg: t.color.status.success.softBg, fg: t.color.status.success.fg };
    case 'danger':
      return { bg: t.color.status.danger.softBg, fg: t.color.status.danger.fg };
    case 'warning':
      return { bg: t.color.status.warning.softBg, fg: t.color.status.warning.fg };
    case 'suggest':
      return { bg: t.color.status.suggest.softBg, fg: t.color.status.suggest.fg };
    case 'accent':
      return { bg: t.color.accent.navBg, fg: t.color.accent.fg };
    case 'info':
    case 'neutral':
    default:
      return { bg: t.color.status.info.softBg, fg: t.color.status.info.fg };
  }
}

export function InlineAlert({ children, tone = 'info', title, action, style }: InlineAlertProps) {
  const t = useTheme();
  const s = createStyles(t);
  const colors = alertColors(t, tone);

  return (
    <View style={[s.alert, { backgroundColor: colors.bg }, style]}>
      <View style={s.body}>
        {title && <Text style={[s.title, { color: colors.fg }]}>{title}</Text>}
        <Text style={[s.message, { color: colors.fg }]}>{children}</Text>
      </View>
      {action}
    </View>
  );
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    alert: {
      flexDirection: 'row',
      alignItems: 'center',
      gap: t.space[3],
      padding: t.space[3],
      borderRadius: t.radius.md,
      marginBottom: t.space[3],
    },
    body: {
      flex: 1,
      gap: t.space[1],
    },
    title: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      fontWeight: t.weight.semibold,
    },
    message: {
      fontFamily: t.font.sans,
      ...t.type.xs,
    },
  });

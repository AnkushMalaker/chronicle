/**
 * Screen — the page shell: safe-area insets, page background, and the scrolling
 * content column every screen shares.
 */

import React from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  View,
  type StyleProp,
  type ViewStyle,
} from 'react-native';
import { SafeAreaView, type Edge } from 'react-native-safe-area-context';

import { useTheme, type Theme } from '@/theme';

interface ScreenProps {
  children: React.ReactNode;
  /** Set false for a screen that manages its own scrolling (e.g. a FlatList). */
  scroll?: boolean;
  /** Lifts content above the keyboard — for screens containing a form. */
  keyboardAvoiding?: boolean;
  /**
   * Which insets to apply. A screen under a navigation header does not need the
   * top inset, since the header already covers it.
   */
  edges?: readonly Edge[];
  contentStyle?: StyleProp<ViewStyle>;
  style?: StyleProp<ViewStyle>;
}

export function Screen({
  children,
  scroll = true,
  keyboardAvoiding = false,
  edges = ['bottom', 'left', 'right'],
  contentStyle,
  style,
}: ScreenProps) {
  const t = useTheme();
  const s = createStyles(t);

  const body = scroll ? (
    <ScrollView contentContainerStyle={[s.content, contentStyle]} keyboardShouldPersistTaps="handled">
      {children}
    </ScrollView>
  ) : (
    <View style={[s.content, s.fill, contentStyle]}>{children}</View>
  );

  return (
    <SafeAreaView style={[s.safe, style]} edges={edges}>
      {keyboardAvoiding ? (
        <KeyboardAvoidingView
          style={s.fill}
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
          keyboardVerticalOffset={Platform.OS === 'ios' ? 100 : 0}
        >
          {body}
        </KeyboardAvoidingView>
      ) : (
        body
      )}
    </SafeAreaView>
  );
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    safe: {
      flex: 1,
      backgroundColor: t.color.surface.page,
    },
    fill: {
      flex: 1,
    },
    content: {
      padding: t.space[4],
      paddingBottom: t.space[10],
    },
  });

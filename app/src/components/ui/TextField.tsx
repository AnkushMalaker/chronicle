/**
 * TextField — a labelled text input on a sunken surface.
 *
 * Mirrors `design-system/components/forms/Input.jsx`, adding the label and hint
 * rows that the mobile forms repeat around every input.
 */

import React, { forwardRef } from 'react';
import {
  StyleSheet,
  Text,
  TextInput,
  View,
  type StyleProp,
  type TextInputProps,
  type ViewStyle,
} from 'react-native';

import { useTheme, type Theme } from '@/theme';

interface TextFieldProps extends Omit<TextInputProps, 'style' | 'placeholderTextColor'> {
  label?: string;
  /** Quiet helper text under the field. */
  hint?: string;
  /** Replaces `hint` and turns the field's border red. */
  error?: string;
  style?: StyleProp<ViewStyle>;
}

export const TextField = forwardRef<TextInput, TextFieldProps>(function TextField(
  { label, hint, error, style, editable = true, ...inputProps },
  ref
) {
  const t = useTheme();
  const s = createStyles(t);

  return (
    <View style={[s.wrapper, style]}>
      {label && <Text style={s.label}>{label}</Text>}
      <TextInput
        ref={ref}
        editable={editable}
        placeholderTextColor={t.color.text.faint}
        style={[s.input, !!error && s.inputError, !editable && s.inputDisabled]}
        {...inputProps}
      />
      {(error || hint) && <Text style={error ? s.error : s.hint}>{error ?? hint}</Text>}
    </View>
  );
});

const createStyles = (t: Theme) =>
  StyleSheet.create({
    wrapper: {
      marginBottom: t.space[3],
    },
    label: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      fontWeight: t.weight.medium,
      color: t.color.text.secondary,
      marginBottom: t.space[1.5],
    },
    input: {
      backgroundColor: t.color.surface.sunken,
      borderWidth: t.borderWidth,
      borderColor: t.color.border.base,
      borderRadius: t.radius.md,
      paddingHorizontal: t.space[3],
      paddingVertical: t.space[2],
      minHeight: 44,
      fontFamily: t.font.sans,
      ...t.type.sm,
      color: t.color.text.primary,
    },
    inputError: {
      borderColor: t.color.status.danger.base,
    },
    inputDisabled: {
      opacity: 0.5,
    },
    hint: {
      fontFamily: t.font.sans,
      ...t.type.xs,
      color: t.color.text.muted,
      marginTop: t.space[1],
    },
    error: {
      fontFamily: t.font.sans,
      ...t.type.xs,
      color: t.color.status.danger.fg,
      marginTop: t.space[1],
    },
  });

// Confirm screen for a screenshot shared into Chronicle from the share sheet.
//
// The share extension hands the image over and opens the app here rather than
// uploading itself. That keeps the JWT in this app's secure storage (no shared
// keychain access group), and it gives a moment to add a caption — which is the
// single most useful signal for finding the image again, and the one thing no
// vision model can reconstruct.

import { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Image,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { useRouter } from 'expo-router';
import { useShareIntent } from 'expo-share-intent';

import { useSharedAppSettings } from '@/contexts/AppSettingsContext';
import { uploadSharedScreenshot } from '@/services/screenshots';
import { useTheme } from '@/theme';

type Phase = 'ready' | 'uploading' | 'done' | 'error';

export default function ShareScreen() {
  const router = useRouter();
  const { colors } = useTheme();
  const { webSocketUrl } = useSharedAppSettings();
  const { hasShareIntent, shareIntent, resetShareIntent } = useShareIntent();

  const [caption, setCaption] = useState('');
  const [phase, setPhase] = useState<Phase>('ready');
  const [message, setMessage] = useState<string | null>(null);

  const imageUri = shareIntent?.files?.[0]?.path ?? null;

  const dismiss = useCallback(() => {
    resetShareIntent();
    router.replace('/');
  }, [resetShareIntent, router]);

  // Nothing to confirm: the intent was consumed or arrived without an image.
  useEffect(() => {
    if (!hasShareIntent && phase === 'ready' && !imageUri) {
      router.replace('/');
    }
  }, [hasShareIntent, imageUri, phase, router]);

  const send = useCallback(async () => {
    if (!imageUri) return;
    setPhase('uploading');
    setMessage(null);
    try {
      const result = await uploadSharedScreenshot(imageUri, webSocketUrl, { caption });
      setPhase('done');
      setMessage(
        result.status === 'duplicate'
          ? 'Already saved to Chronicle.'
          : 'Saved to Chronicle.'
      );
      // Give the confirmation a beat to be read, then get out of the way.
      setTimeout(dismiss, 900);
    } catch (error) {
      // Stay on the screen holding the image so Retry actually has something to
      // retry — there is no background upload queue yet.
      setPhase('error');
      setMessage(error instanceof Error ? error.message : 'Upload failed');
    }
  }, [caption, dismiss, imageUri, webSocketUrl]);

  const busy = phase === 'uploading';

  return (
    <KeyboardAvoidingView
      style={[styles.flex, { backgroundColor: colors.background }]}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView contentContainerStyle={styles.content}>
        {imageUri ? (
          <Image
            source={{ uri: imageUri }}
            style={[styles.preview, { borderColor: colors.cardBorder }]}
            resizeMode="contain"
          />
        ) : (
          <Text style={[styles.muted, { color: colors.textSecondary }]}>
            No image was shared.
          </Text>
        )}

        <Text style={[styles.label, { color: colors.textSecondary }]}>
          Note (optional)
        </Text>
        <TextInput
          value={caption}
          onChangeText={setCaption}
          editable={!busy && phase !== 'done'}
          placeholder="Why you saved this — helps you find it later"
          placeholderTextColor={colors.textTertiary}
          multiline
          style={[
            styles.input,
            {
              backgroundColor: colors.inputBackground,
              borderColor: colors.inputBorder,
              color: colors.text,
            },
          ]}
        />

        {message && (
          <Text
            style={[
              styles.message,
              { color: phase === 'error' ? colors.danger : colors.success },
            ]}
          >
            {message}
          </Text>
        )}

        <View style={styles.actions}>
          <TouchableOpacity
            onPress={dismiss}
            disabled={busy}
            style={[styles.button, styles.secondary, { borderColor: colors.cardBorder }]}
          >
            <Text style={{ color: colors.textSecondary, fontWeight: '600' }}>
              {phase === 'done' ? 'Close' : 'Discard'}
            </Text>
          </TouchableOpacity>

          {phase !== 'done' && (
            <TouchableOpacity
              onPress={send}
              disabled={busy || !imageUri}
              style={[
                styles.button,
                {
                  backgroundColor: imageUri ? colors.primary : colors.disabled,
                },
              ]}
            >
              {busy ? (
                <ActivityIndicator color="#ffffff" />
              ) : (
                <Text style={styles.primaryText}>
                  {phase === 'error' ? 'Retry' : 'Save to Chronicle'}
                </Text>
              )}
            </TouchableOpacity>
          )}
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  content: { padding: 16, gap: 12 },
  preview: {
    width: '100%',
    height: 320,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
  },
  label: { fontSize: 13, fontWeight: '600', marginTop: 4 },
  input: {
    minHeight: 72,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 10,
    padding: 12,
    fontSize: 15,
    textAlignVertical: 'top',
  },
  message: { fontSize: 14, fontWeight: '500' },
  muted: { fontSize: 15, textAlign: 'center', paddingVertical: 32 },
  actions: { flexDirection: 'row', gap: 12, marginTop: 8 },
  button: {
    flex: 1,
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: 'center',
    justifyContent: 'center',
  },
  secondary: { borderWidth: StyleSheet.hairlineWidth },
  primaryText: { color: '#ffffff', fontWeight: '600', fontSize: 15 },
});

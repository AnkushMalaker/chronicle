// Confirm screen for a screenshot shared into Chronicle from the share sheet.
//
// The share extension hands the image over and opens the app here rather than
// uploading itself. That keeps the JWT in this app's secure storage (no shared
// keychain access group), and it gives a moment to add a caption — which is the
// single most useful signal for finding the image again, and the one thing no
// vision model can reconstruct.

import { useCallback, useEffect, useState } from 'react';
import { Image, StyleSheet } from 'react-native';
import { useRouter } from 'expo-router';
import { useShareIntentContext } from 'expo-share-intent';

import { Body, Button, ButtonRow, InlineAlert, Screen, TextField } from '@/components/ui';
import { useSharedAppSettings } from '@/contexts/AppSettingsContext';
import { uploadSharedScreenshot } from '@/services/screenshots';
import { useTheme, type Theme } from '@/theme';

type Phase = 'ready' | 'uploading' | 'done' | 'error';

export default function ShareScreen() {
  const router = useRouter();
  const t = useTheme();
  const s = createStyles(t);
  const { webSocketUrl } = useSharedAppSettings();
  // Shares the root provider's state, so resetting here does not strand the
  // navigator holding a stale copy of the same intent.
  const { isReady, hasShareIntent, shareIntent, resetShareIntent } = useShareIntentContext();

  const [caption, setCaption] = useState('');
  const [phase, setPhase] = useState<Phase>('ready');
  const [message, setMessage] = useState<string | null>(null);

  const imageUri = shareIntent?.files?.[0]?.path ?? null;

  const dismiss = useCallback(() => {
    resetShareIntent();
    router.replace('/');
  }, [resetShareIntent, router]);

  // Nothing to confirm: the intent was consumed or arrived without an image.
  // `isReady` is load-bearing on iOS — the deep link lands here before the
  // native module has been read, so acting sooner bounces straight back home
  // on every share.
  useEffect(() => {
    if (isReady && !hasShareIntent && phase === 'ready' && !imageUri) {
      router.replace('/');
    }
  }, [hasShareIntent, imageUri, isReady, phase, router]);

  const send = useCallback(async () => {
    if (!imageUri) return;
    setPhase('uploading');
    setMessage(null);
    try {
      const result = await uploadSharedScreenshot(imageUri, webSocketUrl, { caption });
      setPhase('done');
      setMessage(
        result.status === 'duplicate' ? 'Already saved to Chronicle.' : 'Saved to Chronicle.'
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
    <Screen keyboardAvoiding>
      {imageUri ? (
        <Image source={{ uri: imageUri }} style={s.preview} resizeMode="contain" />
      ) : (
        <Body style={s.empty}>No image was shared.</Body>
      )}

      <TextField
        label="Note"
        hint="Optional — why you saved this. It is what you will search for later."
        value={caption}
        onChangeText={setCaption}
        editable={!busy && phase !== 'done'}
        placeholder="Game to try, ticket for Friday, fix this error…"
        multiline
        numberOfLines={3}
      />

      {message && (
        <InlineAlert tone={phase === 'error' ? 'danger' : 'success'}>{message}</InlineAlert>
      )}

      <ButtonRow>
        <Button variant="secondary" onPress={dismiss} disabled={busy} style={s.action}>
          {phase === 'done' ? 'Close' : 'Discard'}
        </Button>
        {phase !== 'done' && (
          <Button
            variant="primary"
            onPress={send}
            disabled={!imageUri}
            loading={busy}
            style={s.action}
          >
            {phase === 'error' ? 'Retry' : 'Save to Chronicle'}
          </Button>
        )}
      </ButtonRow>
    </Screen>
  );
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    preview: {
      width: '100%',
      height: 320,
      borderRadius: t.radius.lg,
      borderWidth: t.borderWidth,
      borderColor: t.color.border.subtle,
      backgroundColor: t.color.surface.sunken,
    },
    empty: {
      textAlign: 'center',
      paddingVertical: t.space[8],
      color: t.color.text.secondary,
    },
    action: {
      flex: 1,
    },
  });

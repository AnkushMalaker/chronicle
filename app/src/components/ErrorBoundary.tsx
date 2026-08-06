import React from 'react';
import { Platform, ScrollView, Share, StyleSheet, Text, View } from 'react-native';

import { Button, ButtonRow } from '@/components/ui';
import { useTheme, type Theme } from '@/theme';
import { getLogPath, logError, readLog } from '@/utils/logger';

interface Props {
  children: React.ReactNode;
}

interface State {
  error: Error | null;
  info: React.ErrorInfo | null;
}

interface FallbackProps {
  error: Error;
  info: React.ErrorInfo | null;
  onShare: () => void;
  onReset: () => void;
}

/**
 * The crash screen. Split out as a function component because a class cannot
 * call `useTheme()`.
 */
function ErrorFallback({ error, info, onShare, onReset }: FallbackProps) {
  const t = useTheme();
  const s = createStyles(t);

  return (
    <View style={s.container}>
      <Text style={s.title}>Chronicle crashed</Text>
      <Text style={s.subtitle}>The error has been written to the on-device log.</Text>
      <ScrollView style={s.scroll} contentContainerStyle={s.scrollContent}>
        <Text style={s.errorHeading}>
          {error.name}: {error.message}
        </Text>
        {error.stack ? <Text style={s.stack}>{error.stack}</Text> : null}
        {info?.componentStack ? (
          <>
            <Text style={s.errorHeading}>Component stack</Text>
            <Text style={s.stack}>{info.componentStack}</Text>
          </>
        ) : null}
      </ScrollView>
      <Text style={s.path}>Log: {getLogPath()}</Text>
      <ButtonRow>
        <Button variant="primary" onPress={onShare} style={s.action}>
          Share Log
        </Button>
        <Button variant="secondary" onPress={onReset} style={s.action}>
          Try Again
        </Button>
      </ButtonRow>
    </View>
  );
}

export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null, info: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    this.setState({ info });
    const msg = `React render error: ${error.name}: ${error.message}\ncomponentStack: ${info.componentStack ?? 'unknown'}\nstack: ${error.stack ?? 'no stack'}`;
    logError('ErrorBoundary', msg);
  }

  reset = () => this.setState({ error: null, info: null });

  share = async () => {
    try {
      const contents = await readLog();
      if (Platform.OS === 'ios') {
        await Share.share({ url: `file://${getLogPath()}`, message: contents.slice(-4000) });
      } else {
        await Share.share({ message: contents.slice(-4000) });
      }
    } catch (err) {
      logError('ErrorBoundary', `share failed: ${String(err)}`);
    }
  };

  render() {
    const { error, info } = this.state;
    if (!error) return this.props.children;

    return <ErrorFallback error={error} info={info} onShare={this.share} onReset={this.reset} />;
  }
}

const createStyles = (t: Theme) =>
  StyleSheet.create({
    container: {
      flex: 1,
      backgroundColor: t.color.surface.page,
      padding: t.space[4],
      paddingTop: 60,
    },
    title: {
      fontFamily: t.font.sans,
      color: t.color.status.danger.fg,
      ...t.type['2xl'],
      fontWeight: t.weight.bold,
      marginBottom: t.space[1],
    },
    subtitle: {
      fontFamily: t.font.sans,
      color: t.color.text.secondary,
      ...t.type.sm,
      marginBottom: t.space[3],
    },
    scroll: {
      flex: 1,
      backgroundColor: t.color.surface.sunken,
      borderRadius: t.radius.lg,
    },
    scrollContent: {
      padding: t.space[3],
    },
    errorHeading: {
      fontFamily: t.font.sans,
      color: t.color.text.primary,
      ...t.type.sm,
      fontWeight: t.weight.semibold,
      marginTop: t.space[2],
    },
    stack: {
      fontFamily: t.font.mono,
      color: t.color.text.secondary,
      ...t.type.xs,
      marginTop: t.space[1],
    },
    path: {
      fontFamily: t.font.sans,
      color: t.color.text.muted,
      ...t.type.xs,
      marginTop: t.space[3],
      marginBottom: t.space[1.5],
    },
    action: {
      flex: 1,
    },
  });

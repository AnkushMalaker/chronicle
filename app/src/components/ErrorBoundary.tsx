import React from 'react';
import { View, Text, ScrollView, TouchableOpacity, StyleSheet, Share, Platform } from 'react-native';
import { logError, getLogPath, readLog } from '@/utils/logger';

interface Props {
  children: React.ReactNode;
}

interface State {
  error: Error | null;
  info: React.ErrorInfo | null;
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

    return (
      <View style={styles.container}>
        <Text style={styles.title}>Chronicle crashed</Text>
        <Text style={styles.subtitle}>The error has been written to the on-device log.</Text>
        <ScrollView style={styles.scroll} contentContainerStyle={{ padding: 12 }}>
          <Text style={styles.errorHeading}>{error.name}: {error.message}</Text>
          {error.stack ? <Text style={styles.stack}>{error.stack}</Text> : null}
          {info?.componentStack ? (
            <>
              <Text style={styles.errorHeading}>Component stack</Text>
              <Text style={styles.stack}>{info.componentStack}</Text>
            </>
          ) : null}
        </ScrollView>
        <Text style={styles.path}>Log: {getLogPath()}</Text>
        <View style={styles.row}>
          <TouchableOpacity style={styles.btn} onPress={this.share}>
            <Text style={styles.btnText}>Share Log</Text>
          </TouchableOpacity>
          <TouchableOpacity style={[styles.btn, styles.btnSecondary]} onPress={this.reset}>
            <Text style={styles.btnText}>Try Again</Text>
          </TouchableOpacity>
        </View>
      </View>
    );
  }
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#1c1c1e', padding: 16, paddingTop: 60 },
  title: { color: '#FF3B30', fontSize: 22, fontWeight: '700', marginBottom: 4 },
  subtitle: { color: '#aeaeb2', fontSize: 13, marginBottom: 12 },
  scroll: { flex: 1, backgroundColor: '#2c2c2e', borderRadius: 8 },
  errorHeading: { color: '#fff', fontSize: 14, fontWeight: '600', marginTop: 8 },
  stack: { color: '#d1d1d6', fontSize: 11, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', marginTop: 4 },
  path: { color: '#8e8e93', fontSize: 11, marginTop: 10, marginBottom: 6 },
  row: { flexDirection: 'row', gap: 8 },
  btn: { flex: 1, backgroundColor: '#0A84FF', paddingVertical: 12, borderRadius: 8, alignItems: 'center' },
  btnSecondary: { backgroundColor: '#3a3a3c' },
  btnText: { color: '#fff', fontSize: 15, fontWeight: '600' },
});

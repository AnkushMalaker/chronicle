import React, { useState, useEffect } from 'react';
import { View, Text, TouchableOpacity, StyleSheet, Alert } from 'react-native';

import { Button, Caption, Card, TextField } from '@/components/ui';
import { login, logout, forgetAccount } from '@/services/auth';
import { unregisterPushDevice } from '@/services/pushNotifications';
import { useTheme, type Theme } from '@/theme';
import { getAuthEmail, getAuthPassword } from '@/utils/storage';

interface AuthSectionProps {
  backendUrl: string;
  isAuthenticated: boolean;
  currentUserEmail: string | null;
  onAuthStatusChange: (isAuthenticated: boolean, email: string | null, token: string | null) => void;
}

export const AuthSection: React.FC<AuthSectionProps> = ({
  backendUrl,
  isAuthenticated,
  currentUserEmail,
  onAuthStatusChange,
}) => {
  const t = useTheme();
  const s = createStyles(t);
  const [email, setEmail] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [isLoggingIn, setIsLoggingIn] = useState<boolean>(false);

  useEffect(() => {
    const loadAuthData = async () => {
      const savedEmail = await getAuthEmail();
      const savedPassword = await getAuthPassword();
      if (savedEmail) setEmail(savedEmail);
      if (savedPassword) setPassword(savedPassword);
    };
    loadAuthData();
  }, []);

  const handleLogin = async () => {
    if (!email.trim() || !password.trim()) {
      Alert.alert('Missing Credentials', 'Please enter both email and password.');
      return;
    }
    if (!backendUrl.trim()) {
      Alert.alert('Backend URL Required', 'Please enter a backend URL first.');
      return;
    }

    setIsLoggingIn(true);
    try {
      // login() persists email + password (SecureStore) + token centrally.
      const jwtToken = await login(email.trim(), password.trim(), backendUrl);
      onAuthStatusChange(true, email.trim(), jwtToken);
    } catch (error) {
      Alert.alert('Login Failed', error instanceof Error ? error.message : 'An unknown error occurred during login.');
    } finally {
      setIsLoggingIn(false);
    }
  };

  // Log out: clear the token only. Email + password are kept (in state and in
  // storage) so re-login is one tap and silent refresh keeps working.
  const handleLogout = async () => {
    try {
      await unregisterPushDevice(backendUrl).catch(error => {
        console.warn('[Notifications] unregister before logout failed:', error);
      });
      await logout();
      onAuthStatusChange(false, email || null, null);
    } catch (error) {
      Alert.alert('Logout Error', 'Failed to log out.');
    }
  };

  // Forget account: wipe email + password + token entirely.
  const handleForgetAccount = () => {
    Alert.alert(
      'Forget Account',
      'This clears your saved email and password from this device. You will need to enter them again to log in.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Forget',
          style: 'destructive',
          onPress: async () => {
            try {
              await unregisterPushDevice(backendUrl).catch(error => {
                console.warn('[Notifications] unregister before forget-account failed:', error);
              });
              await forgetAccount();
              setEmail('');
              setPassword('');
              onAuthStatusChange(false, null, null);
            } catch (error) {
              Alert.alert('Error', 'Failed to clear account data.');
            }
          },
        },
      ]
    );
  };

  if (isAuthenticated && currentUserEmail) {
    return (
      <Card title="Authentication">
        <View style={s.authenticatedContainer}>
          <Text style={s.authenticatedText}>Logged in as: {currentUserEmail}</Text>
          <Button variant="danger" size="lg" onPress={handleLogout} disabled={isLoggingIn}>
            Logout
          </Button>
        </View>
        <TouchableOpacity onPress={handleForgetAccount} disabled={isLoggingIn}>
          <Caption style={s.forgetText}>Forget account</Caption>
        </TouchableOpacity>
      </Card>
    );
  }

  return (
    <Card title="Authentication">
      <TextField
        label="Email:"
        value={email}
        onChangeText={setEmail}
        placeholder="user@example.com"
        autoCapitalize="none"
        keyboardType="email-address"
        returnKeyType="next"
        autoCorrect={false}
        editable={!isLoggingIn}
        textContentType="emailAddress"
        autoComplete="email"
      />

      <TextField
        label="Password:"
        value={password}
        onChangeText={setPassword}
        placeholder="Enter your password"
        secureTextEntry={true}
        returnKeyType="go"
        autoCorrect={false}
        editable={!isLoggingIn}
        onSubmitEditing={handleLogin}
        textContentType="password"
        autoComplete="password"
      />

      <Button
        variant="primary"
        size="lg"
        fullWidth
        loading={isLoggingIn}
        onPress={handleLogin}
        style={s.loginButton}
      >
        {isLoggingIn ? 'Logging in...' : 'Login'}
      </Button>

      {!isAuthenticated && (
        <Caption style={s.helpText}>
          Enter your email and password to authenticate with the backend.
        </Caption>
      )}
    </Card>
  );
};

const createStyles = (t: Theme) =>
  StyleSheet.create({
    loginButton: {
      marginTop: t.space[3],
    },
    helpText: {
      marginTop: t.space[3],
      textAlign: 'center',
      fontStyle: 'italic',
    },
    authenticatedContainer: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'center',
    },
    authenticatedText: {
      fontFamily: t.font.sans,
      ...t.type.sm,
      fontWeight: t.weight.medium,
      color: t.color.status.success.fg,
      flex: 1,
      marginRight: t.space[3],
    },
    forgetText: {
      marginTop: t.space[3],
      textAlign: 'center',
      textDecorationLine: 'underline',
    },
  });

export default AuthSection;

import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ActivityIndicator } from 'react-native';
import { saveAuthEmail, saveAuthPassword, saveJwtToken, getAuthEmail, getAuthPassword, clearAuthData } from '../utils/storage';
import { useTheme, ThemeColors } from '../theme';

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
  const { colors } = useTheme();
  const s = createStyles(colors);
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
      const baseUrl = backendUrl.replace('ws://', 'http://').replace('wss://', 'https://').split('/ws')[0];
      const loginUrl = `${baseUrl}/auth/jwt/login`;
      const formData = new URLSearchParams();
      formData.append('username', email.trim());
      formData.append('password', password.trim());

      const response = await fetch(loginUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: formData.toString(),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Login failed: ${response.status} ${response.statusText} - ${errorText}`);
      }

      const authData = await response.json();
      const jwtToken = authData.access_token;
      if (!jwtToken) throw new Error('No access token received from server');

      await saveAuthEmail(email.trim());
      await saveAuthPassword(password.trim());
      await saveJwtToken(jwtToken);
      onAuthStatusChange(true, email.trim(), jwtToken);
    } catch (error) {
      Alert.alert('Login Failed', error instanceof Error ? error.message : 'An unknown error occurred during login.');
    } finally {
      setIsLoggingIn(false);
    }
  };

  const handleLogout = async () => {
    try {
      await clearAuthData();
      setEmail('');
      setPassword('');
      onAuthStatusChange(false, null, null);
    } catch (error) {
      Alert.alert('Logout Error', 'Failed to clear authentication data.');
    }
  };

  if (isAuthenticated && currentUserEmail) {
    return (
      <View style={s.section}>
        <Text style={s.sectionTitle}>Authentication</Text>
        <View style={s.authenticatedContainer}>
          <Text style={s.authenticatedText}>Logged in as: {currentUserEmail}</Text>
          <TouchableOpacity
            style={[s.button, { backgroundColor: colors.danger }]}
            onPress={handleLogout}
            disabled={isLoggingIn}
          >
            <Text style={s.buttonText}>Logout</Text>
          </TouchableOpacity>
        </View>
      </View>
    );
  }

  return (
    <View style={s.section}>
      <Text style={s.sectionTitle}>Authentication</Text>
      <Text style={s.inputLabel}>Email:</Text>
      <TextInput
        style={s.textInput}
        value={email}
        onChangeText={setEmail}
        placeholder="user@example.com"
        placeholderTextColor={colors.textTertiary}
        autoCapitalize="none"
        keyboardType="email-address"
        returnKeyType="next"
        autoCorrect={false}
        editable={!isLoggingIn}
        textContentType="emailAddress"
        autoComplete="email"
      />

      <Text style={s.inputLabel}>Password:</Text>
      <TextInput
        style={s.textInput}
        value={password}
        onChangeText={setPassword}
        placeholder="Enter your password"
        placeholderTextColor={colors.textTertiary}
        secureTextEntry={true}
        returnKeyType="go"
        autoCorrect={false}
        editable={!isLoggingIn}
        onSubmitEditing={handleLogin}
        textContentType="password"
        autoComplete="password"
      />

      <TouchableOpacity
        style={[s.button, isLoggingIn ? s.buttonDisabled : null]}
        onPress={handleLogin}
        disabled={isLoggingIn}
      >
        {isLoggingIn ? (
          <View style={s.loadingContainer}>
            <ActivityIndicator size="small" color="white" />
            <Text style={[s.buttonText, { marginLeft: 8 }]}>Logging in...</Text>
          </View>
        ) : (
          <Text style={s.buttonText}>Login</Text>
        )}
      </TouchableOpacity>

      {!isAuthenticated && (
        <Text style={s.helpText}>
          Enter your email and password to authenticate with the backend.
        </Text>
      )}
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  section: {
    marginBottom: 25,
    padding: 15,
    backgroundColor: colors.card,
    borderRadius: 10,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.1,
    shadowRadius: 3,
    elevation: 2,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 15,
    color: colors.text,
  },
  inputLabel: {
    fontSize: 14,
    color: colors.text,
    marginBottom: 5,
    marginTop: 10,
    fontWeight: '500',
  },
  textInput: {
    backgroundColor: colors.inputBackground,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: 6,
    padding: 10,
    fontSize: 14,
    width: '100%',
    marginBottom: 10,
    color: colors.text,
  },
  button: {
    backgroundColor: colors.primary,
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    marginTop: 15,
    elevation: 2,
  },
  buttonDisabled: {
    backgroundColor: colors.disabled,
    opacity: 0.7,
  },
  buttonText: {
    color: 'white',
    fontSize: 16,
    fontWeight: '600',
  },
  loadingContainer: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  helpText: {
    fontSize: 12,
    color: colors.textTertiary,
    marginTop: 10,
    textAlign: 'center',
    fontStyle: 'italic',
  },
  authenticatedContainer: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  authenticatedText: {
    fontSize: 14,
    color: colors.success,
    fontWeight: '500',
    flex: 1,
    marginRight: 10,
  },
});

export default AuthSection;

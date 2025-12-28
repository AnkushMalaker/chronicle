import React, { useState, useEffect, useCallback } from 'react';
import {
  View,
  Text,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  Modal,
  ScrollView,
  KeyboardAvoidingView,
  Platform,
  ActivityIndicator,
} from 'react-native';
import theme from '../theme/design-system';
import type { ServerConnection, Protocol, Route } from '../types/serverConnection';
import { createEmptyConnection, generateConnectionId } from '../types/serverConnection';

type ValidationStatus = 'idle' | 'checking' | 'valid' | 'invalid' | 'auth_failed';

interface ServerConnectionFormProps {
  visible: boolean;
  onClose: () => void;
  onSave: (connection: ServerConnection) => void;
  editConnection?: ServerConnection | null;
}

const PROTOCOLS: { label: string; value: Protocol }[] = [
  { label: 'wss://', value: 'wss' },
  { label: 'ws://', value: 'ws' },
  { label: 'https://', value: 'https' },
  { label: 'http://', value: 'http' },
];

const ROUTES: { label: string; value: Route }[] = [
  { label: '/ws_pcm', value: 'ws_pcm' },
  { label: '/ws_omi', value: 'ws_omi' },
  { label: '/ws', value: 'ws' },
  { label: '(none)', value: '' },
];

// Simple dropdown picker component
const Picker: React.FC<{
  options: { label: string; value: string }[];
  value: string;
  onChange: (value: string) => void;
  testID: string;
}> = ({ options, value, onChange, testID }) => {
  const [isOpen, setIsOpen] = useState(false);
  const selectedOption = options.find(o => o.value === value);

  return (
    <View style={pickerStyles.container}>
      <TouchableOpacity
        style={pickerStyles.button}
        onPress={() => setIsOpen(!isOpen)}
        testID={testID}
      >
        <Text style={pickerStyles.buttonText}>{selectedOption?.label || value}</Text>
        <Text style={pickerStyles.arrow}>{isOpen ? '▲' : '▼'}</Text>
      </TouchableOpacity>
      {isOpen && (
        <View style={pickerStyles.dropdown}>
          {options.map((option) => (
            <TouchableOpacity
              key={option.value}
              style={[
                pickerStyles.option,
                option.value === value && pickerStyles.optionActive,
              ]}
              onPress={() => {
                onChange(option.value);
                setIsOpen(false);
              }}
              testID={`${testID}-option-${option.value || 'none'}`}
            >
              <Text
                style={[
                  pickerStyles.optionText,
                  option.value === value && pickerStyles.optionTextActive,
                ]}
              >
                {option.label}
              </Text>
            </TouchableOpacity>
          ))}
        </View>
      )}
    </View>
  );
};

export const ServerConnectionForm: React.FC<ServerConnectionFormProps> = ({
  visible,
  onClose,
  onSave,
  editConnection,
}) => {
  const [name, setName] = useState('');
  const [protocol, setProtocol] = useState<Protocol>('wss');
  const [hostWithPort, setHostWithPort] = useState(''); // Combined domain:port
  const [route, setRoute] = useState<Route>('ws_pcm');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [validationStatus, setValidationStatus] = useState<ValidationStatus>('idle');
  const [validationMessage, setValidationMessage] = useState('');
  const [isSaving, setIsSaving] = useState(false);

  // Parse domain and port from combined field
  const parseHostWithPort = (value: string): { domain: string; port: string } => {
    const parts = value.split(':');
    if (parts.length === 2 && /^\d+$/.test(parts[1])) {
      return { domain: parts[0], port: parts[1] };
    }
    return { domain: value, port: '' };
  };

  // Build HTTP URL for health checks
  const buildHttpUrl = useCallback((proto: Protocol, host: string): string => {
    const { domain, port } = parseHostWithPort(host);
    const httpProtocol = proto === 'wss' ? 'https' : proto === 'ws' ? 'http' : proto;
    let url = `${httpProtocol}://${domain}`;
    if (port) url += `:${port}`;
    return url;
  }, []);

  // Validate server reachability
  const validateServer = useCallback(async () => {
    const { domain } = parseHostWithPort(hostWithPort);
    if (!domain.trim()) {
      setValidationStatus('idle');
      setValidationMessage('');
      return false;
    }

    setValidationStatus('checking');
    setValidationMessage('Checking server...');

    try {
      const httpUrl = buildHttpUrl(protocol, hostWithPort);
      const response = await fetch(`${httpUrl}/health`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
      });

      if (response.ok) {
        setValidationStatus('valid');
        setValidationMessage('Server reachable');
        return true;
      } else {
        setValidationStatus('invalid');
        setValidationMessage(`Server returned ${response.status}`);
        return false;
      }
    } catch (error) {
      setValidationStatus('invalid');
      setValidationMessage('Cannot reach server');
      return false;
    }
  }, [hostWithPort, protocol, buildHttpUrl]);

  // Authenticate with server
  const authenticateWithServer = useCallback(async (): Promise<boolean> => {
    if (!username.trim() || !password.trim()) {
      return true; // No auth needed
    }

    const httpUrl = buildHttpUrl(protocol, hostWithPort);
    try {
      const response = await fetch(`${httpUrl}/auth/jwt/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `username=${encodeURIComponent(username.trim())}&password=${encodeURIComponent(password.trim())}`,
      });

      if (response.ok) {
        return true;
      } else {
        setValidationStatus('auth_failed');
        setValidationMessage('Invalid credentials');
        return false;
      }
    } catch (error) {
      setValidationStatus('auth_failed');
      setValidationMessage('Authentication failed');
      return false;
    }
  }, [username, password, protocol, hostWithPort, buildHttpUrl]);

  // Reset form when opening/closing or editing different connection
  useEffect(() => {
    if (visible) {
      setValidationStatus('idle');
      setValidationMessage('');
      setIsSaving(false);

      if (editConnection) {
        setName(editConnection.name);
        setProtocol(editConnection.protocol);
        const combined = editConnection.port
          ? `${editConnection.domain}:${editConnection.port}`
          : editConnection.domain;
        setHostWithPort(combined);
        setRoute(editConnection.route);
        setUsername(editConnection.username);
        setPassword(editConnection.password);
      } else {
        const empty = createEmptyConnection();
        setName(empty.name);
        setProtocol(empty.protocol);
        setHostWithPort('');
        setRoute(empty.route);
        setUsername(empty.username);
        setPassword(empty.password);
      }
    }
  }, [visible, editConnection]);

  // Handle blur on host field - validate server
  const handleHostBlur = useCallback(() => {
    validateServer();
  }, [validateServer]);

  const handleSave = async () => {
    const { domain, port } = parseHostWithPort(hostWithPort);
    if (!name.trim() || !domain.trim()) {
      return;
    }

    setIsSaving(true);

    // First check server is reachable
    const serverReachable = await validateServer();
    if (!serverReachable) {
      setIsSaving(false);
      return;
    }

    // If credentials provided, verify they work
    if (username.trim() && password.trim()) {
      const authSuccess = await authenticateWithServer();
      if (!authSuccess) {
        setIsSaving(false);
        return;
      }
    }

    const now = Date.now();
    const connection: ServerConnection = {
      id: editConnection?.id || generateConnectionId(),
      name: name.trim(),
      protocol,
      domain: domain.trim(),
      port: port.trim(),
      route,
      username: username.trim(),
      password: password.trim(),
      createdAt: editConnection?.createdAt || now,
      updatedAt: now,
    };

    setIsSaving(false);
    onSave(connection);
    onClose();
  };

  const { domain } = parseHostWithPort(hostWithPort);
  const isValid = name.trim() && domain.trim();
  const canSave = isValid && validationStatus !== 'checking' && !isSaving;

  // Build preview URL
  const { domain: previewDomain, port: previewPort } = parseHostWithPort(hostWithPort);
  const previewUrl = `${protocol}://${previewDomain || 'host'}${previewPort ? `:${previewPort}` : ''}${route ? `/${route}` : ''}`;

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent={true}
      onRequestClose={onClose}
    >
      <KeyboardAvoidingView
        style={styles.modalOverlay}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <View style={styles.modalContent}>
          <ScrollView showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled">
            <Text style={styles.modalTitle}>
              {editConnection ? 'Edit Server' : 'Add Server'}
            </Text>

            {/* Server Name */}
            <Text style={styles.inputLabel}>Server Name</Text>
            <TextInput
              style={styles.textInput}
              value={name}
              onChangeText={setName}
              placeholder="My Chronicle Server"
              autoCapitalize="words"
              testID="server-name-input"
            />

            {/* Connection URL Row */}
            <Text style={styles.inputLabel}>Connection URL</Text>
            <View style={styles.urlRow}>
              <View style={styles.protocolPicker}>
                <Picker
                  options={PROTOCOLS}
                  value={protocol}
                  onChange={(v) => setProtocol(v as Protocol)}
                  testID="protocol-picker"
                />
              </View>
              <TextInput
                style={[styles.textInput, styles.hostInput]}
                value={hostWithPort}
                onChangeText={setHostWithPort}
                onBlur={handleHostBlur}
                placeholder="host:port"
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="url"
                testID="server-host-input"
              />
              <View style={styles.routePicker}>
                <Picker
                  options={ROUTES}
                  value={route}
                  onChange={(v) => setRoute(v as Route)}
                  testID="route-picker"
                />
              </View>
            </View>

            {/* URL Preview with Validation Status */}
            <View style={styles.previewContainer}>
              <View style={styles.previewRow}>
                <Text style={styles.previewUrl} numberOfLines={1}>
                  {previewUrl}
                </Text>
                {validationStatus === 'checking' && (
                  <ActivityIndicator size="small" color={theme.colors.text.secondary} />
                )}
                {validationStatus === 'valid' && (
                  <Text style={styles.validIcon}>✓</Text>
                )}
                {(validationStatus === 'invalid' || validationStatus === 'auth_failed') && (
                  <Text style={styles.invalidIcon}>✗</Text>
                )}
              </View>
              {validationMessage ? (
                <Text style={[
                  styles.validationMessage,
                  validationStatus === 'valid' && styles.validationSuccess,
                  (validationStatus === 'invalid' || validationStatus === 'auth_failed') && styles.validationError,
                ]}>
                  {validationMessage}
                </Text>
              ) : null}
            </View>

            {/* Authentication */}
            <Text style={styles.sectionHeader}>Authentication (optional)</Text>
            <View style={styles.authRow}>
              <TextInput
                style={[styles.textInput, styles.authInput]}
                value={username}
                onChangeText={setUsername}
                placeholder="Email"
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="email-address"
                textContentType="username"
                testID="server-username-input"
              />
              <TextInput
                style={[styles.textInput, styles.authInput]}
                value={password}
                onChangeText={setPassword}
                placeholder="Password"
                secureTextEntry
                textContentType="password"
                testID="server-password-input"
              />
            </View>

            {/* Action Buttons */}
            <View style={styles.buttonRow}>
              <TouchableOpacity
                style={[styles.button, styles.buttonSecondary]}
                onPress={onClose}
                disabled={isSaving}
                testID="cancel-button"
              >
                <Text style={styles.buttonSecondaryText}>Cancel</Text>
              </TouchableOpacity>

              <TouchableOpacity
                style={[styles.button, styles.buttonPrimary, !canSave && styles.buttonDisabled]}
                onPress={handleSave}
                disabled={!canSave}
                testID="save-button"
              >
                {isSaving ? (
                  <ActivityIndicator size="small" color={theme.colors.text.inverse} />
                ) : (
                  <Text style={styles.buttonPrimaryText}>
                    {editConnection ? 'Update' : 'Save'}
                  </Text>
                )}
              </TouchableOpacity>
            </View>
          </ScrollView>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
};

const pickerStyles = StyleSheet.create({
  container: {
    position: 'relative',
    zIndex: 10,
  },
  button: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: theme.colors.gray[100],
    paddingVertical: theme.spacing.sm + 2,
    paddingHorizontal: theme.spacing.sm,
    borderRadius: theme.borderRadius.sm,
    borderWidth: 1,
    borderColor: theme.colors.border.light,
    minWidth: 80,
  },
  buttonText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.primary,
    fontWeight: theme.typography.fontWeight.medium,
  },
  arrow: {
    fontSize: 10,
    color: theme.colors.text.tertiary,
    marginLeft: 4,
  },
  dropdown: {
    position: 'absolute',
    top: '100%',
    left: 0,
    right: 0,
    backgroundColor: theme.colors.background.primary,
    borderRadius: theme.borderRadius.sm,
    borderWidth: 1,
    borderColor: theme.colors.border.light,
    ...theme.shadows.md,
    zIndex: 100,
    marginTop: 2,
  },
  option: {
    paddingVertical: theme.spacing.sm,
    paddingHorizontal: theme.spacing.sm,
    borderBottomWidth: 1,
    borderBottomColor: theme.colors.border.light,
  },
  optionActive: {
    backgroundColor: theme.colors.primary.dark,
  },
  optionText: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.primary,
  },
  optionTextActive: {
    color: theme.colors.primary.main,
    fontWeight: theme.typography.fontWeight.semibold,
  },
});

const styles = StyleSheet.create({
  modalOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0, 0, 0, 0.5)',
    justifyContent: 'flex-end',
  },
  modalContent: {
    backgroundColor: theme.colors.background.primary,
    borderTopLeftRadius: theme.borderRadius.xl,
    borderTopRightRadius: theme.borderRadius.xl,
    padding: theme.spacing.lg,
    maxHeight: '70%',
  },
  modalTitle: {
    fontSize: theme.typography.fontSize.lg,
    fontWeight: theme.typography.fontWeight.bold,
    color: theme.colors.text.primary,
    marginBottom: theme.spacing.md,
    textAlign: 'center',
  },
  inputLabel: {
    fontSize: theme.typography.fontSize.sm,
    color: theme.colors.text.secondary,
    marginBottom: theme.spacing.xs,
    marginTop: theme.spacing.sm,
    fontWeight: theme.typography.fontWeight.medium,
  },
  textInput: {
    ...theme.components.input,
    marginBottom: theme.spacing.xs,
  },
  urlRow: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: theme.spacing.xs,
    zIndex: 20,
  },
  protocolPicker: {
    zIndex: 30,
  },
  hostInput: {
    flex: 1,
  },
  routePicker: {
    zIndex: 25,
  },
  previewContainer: {
    marginTop: theme.spacing.sm,
    padding: theme.spacing.sm,
    backgroundColor: theme.colors.gray[50],
    borderRadius: theme.borderRadius.sm,
    borderWidth: 1,
    borderColor: theme.colors.border.light,
  },
  previewRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  previewUrl: {
    flex: 1,
    fontSize: theme.typography.fontSize.xs,
    color: theme.colors.text.secondary,
    fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace',
  },
  validIcon: {
    fontSize: 16,
    color: theme.colors.status.healthy,
    fontWeight: 'bold' as const,
    marginLeft: theme.spacing.sm,
  },
  invalidIcon: {
    fontSize: 16,
    color: theme.colors.status.unhealthy,
    fontWeight: 'bold' as const,
    marginLeft: theme.spacing.sm,
  },
  validationMessage: {
    fontSize: theme.typography.fontSize.xs,
    marginTop: theme.spacing.xs,
    color: theme.colors.text.secondary,
  },
  validationSuccess: {
    color: theme.colors.status.healthy,
  },
  validationError: {
    color: theme.colors.status.unhealthy,
  },
  sectionHeader: {
    fontSize: theme.typography.fontSize.sm,
    fontWeight: theme.typography.fontWeight.medium,
    color: theme.colors.text.secondary,
    marginTop: theme.spacing.md,
    marginBottom: theme.spacing.xs,
  },
  authRow: {
    flexDirection: 'row',
    gap: theme.spacing.sm,
  },
  authInput: {
    flex: 1,
  },
  buttonRow: {
    flexDirection: 'row',
    gap: theme.spacing.md,
    marginTop: theme.spacing.lg,
    marginBottom: theme.spacing.md,
  },
  button: {
    flex: 1,
    paddingVertical: theme.spacing.md,
    borderRadius: theme.borderRadius.md,
    alignItems: 'center',
  },
  buttonPrimary: {
    backgroundColor: theme.colors.primary.main,
  },
  buttonSecondary: {
    backgroundColor: theme.colors.gray[100],
  },
  buttonDisabled: {
    backgroundColor: theme.colors.gray[300],
    borderWidth: 1,
    borderColor: theme.colors.border.medium,
  },
  buttonPrimaryText: {
    color: theme.colors.primary.contrast,  // Dark text for WCAG AA
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.semibold,
  },
  buttonSecondaryText: {
    color: theme.colors.text.secondary,
    fontSize: theme.typography.fontSize.md,
    fontWeight: theme.typography.fontWeight.medium,
  },
});

export default ServerConnectionForm;

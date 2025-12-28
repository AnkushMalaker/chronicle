import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ActivityIndicator } from 'react-native';

interface BackendStatusProps {
  backendUrl: string;
  onBackendUrlChange: (url: string) => void;
  jwtToken: string | null;
}

interface HealthStatus {
  status: 'unknown' | 'checking' | 'healthy' | 'unhealthy' | 'auth_required';
  message: string;
  lastChecked?: Date;
}

export const BackendStatus: React.FC<BackendStatusProps> = ({
  backendUrl,
  onBackendUrlChange,
  jwtToken,
}) => {
  const [healthStatus, setHealthStatus] = useState<HealthStatus>({
    status: 'unknown',
    message: 'Not checked',
  });

  const checkBackendHealth = async (showAlert: boolean = false) => {
    if (!backendUrl.trim()) {
      setHealthStatus({
        status: 'unhealthy',
        message: 'Backend URL not set',
      });
      return;
    }

    setHealthStatus({
      status: 'checking',
      message: 'Checking connection...',
    });

    try {
      // Convert WebSocket URL to HTTP URL for health check
      let baseUrl = backendUrl.trim();
      
      // Handle different URL formats
      if (baseUrl.startsWith('ws://')) {
        baseUrl = baseUrl.replace('ws://', 'http://');
      } else if (baseUrl.startsWith('wss://')) {
        baseUrl = baseUrl.replace('wss://', 'https://');
      }
      
      // Remove any WebSocket path if present
      baseUrl = baseUrl.split('/ws')[0];
      
      // Try health endpoint first
      const healthUrl = `${baseUrl}/health`;
      console.log('[BackendStatus] Checking health at:', healthUrl);
      
      const response = await fetch(healthUrl, {
        method: 'GET',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
          ...(jwtToken ? { 'Authorization': `Bearer ${jwtToken}` } : {}),
        },
      });
      
      console.log('[BackendStatus] Health check response status:', response.status);

      if (response.ok) {
        const healthData = await response.json();
        setHealthStatus({
          status: 'healthy',
          message: `Connected (${healthData.status || 'OK'})`,
          lastChecked: new Date(),
        });
        
        if (showAlert) {
          Alert.alert('Connection Success', 'Successfully connected to backend!');
        }
      } else if (response.status === 401 || response.status === 403) {
        setHealthStatus({
          status: 'auth_required',
          message: 'Authentication required',
          lastChecked: new Date(),
        });
        
        if (showAlert) {
          Alert.alert('Authentication Required', 'Please login to access the backend.');
        }
      } else {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
    } catch (error) {
      console.error('[BackendStatus] Health check error:', error);
      
      let errorMessage = 'Connection failed';
      if (error instanceof Error) {
        if (error.message.includes('Network request failed')) {
          errorMessage = 'Network request failed - check URL and network connection';
        } else if (error.name === 'AbortError') {
          errorMessage = 'Request timeout';
        } else {
          errorMessage = error.message;
        }
      }
      
      setHealthStatus({
        status: 'unhealthy',
        message: errorMessage,
        lastChecked: new Date(),
      });
      
      if (showAlert) {
        Alert.alert(
          'Connection Failed',
          `Could not connect to backend: ${errorMessage}\n\nMake sure the backend is running and accessible.`
        );
      }
    }
  };

  // Auto-check health when backend URL or JWT token changes
  useEffect(() => {
    if (backendUrl.trim()) {
      const timer = setTimeout(() => {
        checkBackendHealth(false);
      }, 500); // Debounce
      
      return () => clearTimeout(timer);
    }
  }, [backendUrl, jwtToken]);

  const getStatusColor = (status: HealthStatus['status']): string => {
    switch (status) {
      case 'healthy':
        return '#4CD964';
      case 'checking':
        return '#FF9500';
      case 'unhealthy':
        return '#FF3B30';
      case 'auth_required':
        return '#FF9500';
      default:
        return '#8E8E93';
    }
  };

  const getStatusIcon = (status: HealthStatus['status']): string => {
    switch (status) {
      case 'healthy':
        return '✅';
      case 'checking':
        return '🔄';
      case 'unhealthy':
        return '❌';
      case 'auth_required':
        return '🔐';
      default:
        return '❓';
    }
  };

  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>Backend Connection</Text>
      
      <Text style={styles.inputLabel}>Backend URL:</Text>
      <TextInput
        style={styles.textInput}
        value={backendUrl}
        onChangeText={onBackendUrlChange}
        placeholder="ws://localhost:8000/ws (simple) or ws://localhost:8080/v1/listen (advanced)"
        autoCapitalize="none"
        keyboardType="url"
        returnKeyType="done"
        autoCorrect={false}
      />

      <View style={styles.statusContainer}>
        <View style={styles.statusRow}>
          <Text style={styles.statusLabel}>Status:</Text>
          <View style={styles.statusValue}>
            <Text style={styles.statusIcon}>{getStatusIcon(healthStatus.status)}</Text>
            <Text style={[styles.statusText, { color: getStatusColor(healthStatus.status) }]}>
              {healthStatus.message}
            </Text>
            {healthStatus.status === 'checking' && (
              <ActivityIndicator size="small" color={getStatusColor(healthStatus.status)} style={{ marginLeft: 8 }} />
            )}
          </View>
        </View>

        {healthStatus.lastChecked && (
          <Text style={styles.lastCheckedText}>
            Last checked: {healthStatus.lastChecked.toLocaleTimeString()}
          </Text>
        )}
      </View>

      <TouchableOpacity
        style={[styles.button, healthStatus.status === 'checking' ? styles.buttonDisabled : null]}
        onPress={() => checkBackendHealth(true)}
        disabled={healthStatus.status === 'checking'}
      >
        <Text style={styles.buttonText}>
          {healthStatus.status === 'checking' ? 'Checking...' : 'Test Connection'}
        </Text>
      </TouchableOpacity>

      <Text style={styles.helpText}>
        Enter the WebSocket URL of your backend server. Simple backend: http://localhost:8000/ (no auth). 
        Advanced backend: http://localhost:8080/ (requires login). Status is automatically checked.
        The websocket URL can be different or the same as the HTTP URL, with /ws_omi suffix
      </Text>
    </View>
  );
};

const styles = StyleSheet.create({
  section: {
    marginBottom: 25,
    padding: 15,
    backgroundColor: 'white',
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
    color: '#333',
  },
  inputLabel: {
    fontSize: 14,
    color: '#333',
    marginBottom: 5,
    fontWeight: '500',
  },
  textInput: {
    backgroundColor: '#f0f0f0',
    borderWidth: 1,
    borderColor: '#ddd',
    borderRadius: 6,
    padding: 10,
    fontSize: 14,
    width: '100%',
    marginBottom: 15,
    color: '#333',
  },
  statusContainer: {
    marginBottom: 15,
    padding: 10,
    backgroundColor: '#f8f9fa',
    borderRadius: 6,
    borderWidth: 1,
    borderColor: '#e9ecef',
  },
  statusRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  statusLabel: {
    fontSize: 14,
    fontWeight: '500',
    color: '#333',
  },
  statusValue: {
    flexDirection: 'row',
    alignItems: 'center',
    flex: 1,
    justifyContent: 'flex-end',
  },
  statusIcon: {
    fontSize: 16,
    marginRight: 6,
  },
  statusText: {
    fontSize: 14,
    fontWeight: '500',
  },
  lastCheckedText: {
    fontSize: 12,
    color: '#666',
    marginTop: 5,
    textAlign: 'center',
    fontStyle: 'italic',
  },
  button: {
    backgroundColor: '#007AFF',
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    marginBottom: 10,
    elevation: 2,
  },
  buttonDisabled: {
    backgroundColor: '#A0A0A0',
    opacity: 0.7,
  },
  buttonText: {
    color: 'white',
    fontSize: 16,
    fontWeight: '600',
  },
  helpText: {
    fontSize: 12,
    color: '#666',
    textAlign: 'center',
    fontStyle: 'italic',
  },
});

export default BackendStatus;
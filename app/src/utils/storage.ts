import AsyncStorage from '@react-native-async-storage/async-storage';
import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';

const LAST_CONNECTED_DEVICE_ID_KEY = 'LAST_CONNECTED_DEVICE_ID';
const WEBSOCKET_URL_KEY = 'WEBSOCKET_URL_KEY';
const DEEPGRAM_API_KEY_KEY = 'DEEPGRAM_API_KEY_KEY';
const USER_ID_KEY = 'USER_ID_KEY';
const AUTH_EMAIL_KEY = 'AUTH_EMAIL_KEY';
const SERVICE_MANAGER_URL_KEY = 'SERVICE_MANAGER_URL_KEY';
const AUTO_RECONNECT_ENABLED_KEY = 'AUTO_RECONNECT_ENABLED_KEY';
const THEME_PREFERENCE_KEY = 'THEME_PREFERENCE_KEY';
// SecureStore keys must be alphanumeric + ._- (no other punctuation).
const AUTH_PASSWORD_KEY = 'AUTH_PASSWORD_KEY';
const JWT_TOKEN_KEY = 'JWT_TOKEN_KEY';
const SERVICE_MANAGER_TOKEN_KEY = 'SERVICE_MANAGER_TOKEN_KEY';
const INSTALLATION_ID_KEY = 'CHRONICLE_INSTALLATION_ID';

/**
 * Secret storage helpers. Secrets (password, JWT) live in the OS keychain via
 * expo-secure-store on native. SecureStore is unavailable on web, so there we
 * fall back to AsyncStorage (the web build is dev-only).
 */
const secureAvailable = Platform.OS !== 'web';

const secureSet = async (key: string, value: string): Promise<void> => {
  if (secureAvailable) {
    await SecureStore.setItemAsync(key, value);
  } else {
    await AsyncStorage.setItem(key, value);
  }
};

const secureGet = async (key: string): Promise<string | null> => {
  if (secureAvailable) {
    return await SecureStore.getItemAsync(key);
  }
  return await AsyncStorage.getItem(key);
};

const secureRemove = async (key: string): Promise<void> => {
  if (secureAvailable) {
    await SecureStore.deleteItemAsync(key);
  } else {
    await AsyncStorage.removeItem(key);
  }
};

/** Stable app-install identity, intentionally separate from BLE/wearable ids. */
export const getOrCreateInstallationId = async (): Promise<string> => {
  const existing = await secureGet(INSTALLATION_ID_KEY);
  if (existing) return existing;
  const random = Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));
  random[6] = (random[6] & 0x0f) | 0x40;
  random[8] = (random[8] & 0x3f) | 0x80;
  const hex = random.map(value => value.toString(16).padStart(2, '0')).join('');
  const created = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  await secureSet(INSTALLATION_ID_KEY, created);
  return created;
};

export const saveLastConnectedDeviceId = async (deviceId: string | null): Promise<void> => {
  try {
    if (deviceId) {
      await AsyncStorage.setItem(LAST_CONNECTED_DEVICE_ID_KEY, deviceId);
      console.log('[Storage] Last connected device ID saved:', deviceId);
    } else {
      await AsyncStorage.removeItem(LAST_CONNECTED_DEVICE_ID_KEY);
      console.log('[Storage] Last connected device ID removed.');
    }
  } catch (error) {
    console.error('[Storage] Error saving last connected device ID:', error);
  }
};

export const getLastConnectedDeviceId = async (): Promise<string | null> => {
  try {
    const deviceId = await AsyncStorage.getItem(LAST_CONNECTED_DEVICE_ID_KEY);
    console.log('[Storage] Raw value from AsyncStorage.getItem for device ID:', deviceId === null ? "null" : `"${deviceId}"`);
    return deviceId;
  } catch (error) {
    console.error('[Storage] Error retrieving last connected device ID:', error);
    return null;
  }
};

// WebSocket URL
export const saveWebSocketUrl = async (url: string | null): Promise<void> => {
  try {
    if (url) {
      await AsyncStorage.setItem(WEBSOCKET_URL_KEY, url);
      console.log('[Storage] WebSocket URL saved:', url);
    } else {
      await AsyncStorage.removeItem(WEBSOCKET_URL_KEY);
      console.log('[Storage] WebSocket URL removed.');
    }
  } catch (error) {
    console.error('[Storage] Error saving WebSocket URL:', error);
  }
};

export const getWebSocketUrl = async (): Promise<string | null> => {
  try {
    const url = await AsyncStorage.getItem(WEBSOCKET_URL_KEY);
    console.log('[Storage] Retrieved WebSocket URL:', url);
    return url;
  } catch (error) {
    console.error('[Storage] Error retrieving WebSocket URL:', error);
    return null;
  }
};

// Deepgram API Key
export const saveDeepgramApiKey = async (apiKey: string | null): Promise<void> => {
  try {
    if (apiKey) {
      await AsyncStorage.setItem(DEEPGRAM_API_KEY_KEY, apiKey);
    } else {
      await AsyncStorage.removeItem(DEEPGRAM_API_KEY_KEY);
    }
  } catch (error) {
    throw error;
  }
};

export const getDeepgramApiKey = async (): Promise<string | null> => {
  try {
    const apiKey = await AsyncStorage.getItem(DEEPGRAM_API_KEY_KEY);
    return apiKey;
  } catch (error) {
    throw error;
  }
};

// User ID
export const saveUserId = async (userId: string | null): Promise<void> => {
  try {
    if (userId) {
      await AsyncStorage.setItem(USER_ID_KEY, userId);
      console.log('[Storage] User ID saved:', userId);
    } else {
      await AsyncStorage.removeItem(USER_ID_KEY);
      console.log('[Storage] User ID removed.');
    }
  } catch (error) {
    console.error('[Storage] Error saving User ID:', error);
  }
};

export const getUserId = async (): Promise<string | null> => {
  try {
    const userId = await AsyncStorage.getItem(USER_ID_KEY);
    console.log('[Storage] Retrieved User ID:', userId);
    return userId;
  } catch (error) {
    console.error('[Storage] Error retrieving User ID:', error);
    return null;
  }
};

// Authentication Email
export const saveAuthEmail = async (email: string | null): Promise<void> => {
  try {
    if (email) {
      await AsyncStorage.setItem(AUTH_EMAIL_KEY, email);
      console.log('[Storage] Auth email saved:', email);
    } else {
      await AsyncStorage.removeItem(AUTH_EMAIL_KEY);
      console.log('[Storage] Auth email removed.');
    }
  } catch (error) {
    console.error('[Storage] Error saving auth email:', error);
  }
};

export const getAuthEmail = async (): Promise<string | null> => {
  try {
    const email = await AsyncStorage.getItem(AUTH_EMAIL_KEY);
    console.log('[Storage] Retrieved auth email:', email);
    return email;
  } catch (error) {
    console.error('[Storage] Error retrieving auth email:', error);
    return null;
  }
};

// Authentication Password (SecureStore-backed)
export const saveAuthPassword = async (password: string | null): Promise<void> => {
  try {
    if (password) {
      await secureSet(AUTH_PASSWORD_KEY, password);
      console.log('[Storage] Auth password saved.'); // Don't log password for security
    } else {
      await secureRemove(AUTH_PASSWORD_KEY);
      console.log('[Storage] Auth password removed.');
    }
  } catch (error) {
    console.error('[Storage] Error saving auth password:', error);
  }
};

export const getAuthPassword = async (): Promise<string | null> => {
  try {
    const password = await secureGet(AUTH_PASSWORD_KEY);
    if (password) {
      console.log('[Storage] Retrieved auth password.');
    }
    return password;
  } catch (error) {
    console.error('[Storage] Error retrieving auth password:', error);
    return null;
  }
};

// JWT Token (SecureStore-backed)
export const saveJwtToken = async (token: string | null): Promise<void> => {
  try {
    if (token) {
      await secureSet(JWT_TOKEN_KEY, token);
      console.log('[Storage] JWT token saved.');
    } else {
      await secureRemove(JWT_TOKEN_KEY);
      console.log('[Storage] JWT token removed.');
    }
  } catch (error) {
    console.error('[Storage] Error saving JWT token:', error);
  }
};

export const getJwtToken = async (): Promise<string | null> => {
  try {
    const token = await secureGet(JWT_TOKEN_KEY);
    if (token) {
      console.log('[Storage] Retrieved JWT token.');
    }
    return token;
  } catch (error) {
    console.error('[Storage] Error retrieving JWT token:', error);
    return null;
  }
};

// Auto-reconnect preference: true (default) = stay connected / persistent
// reconnect; false = connect once (no auto-reconnect on drop).
export const saveAutoReconnectEnabled = async (enabled: boolean): Promise<void> => {
  try {
    await AsyncStorage.setItem(AUTO_RECONNECT_ENABLED_KEY, enabled ? '1' : '0');
  } catch (error) {
    console.error('[Storage] Error saving auto-reconnect preference:', error);
  }
};

export const getAutoReconnectEnabled = async (): Promise<boolean> => {
  try {
    const v = await AsyncStorage.getItem(AUTO_RECONNECT_ENABLED_KEY);
    return v === null ? true : v === '1'; // default ON
  } catch (error) {
    console.error('[Storage] Error retrieving auto-reconnect preference:', error);
    return true;
  }
};

// Appearance preference: 'system' (default) follows the OS, 'light'/'dark' pin it.
export type ThemePreference = 'system' | 'light' | 'dark';

export const saveThemePreference = async (preference: ThemePreference): Promise<void> => {
  try {
    await AsyncStorage.setItem(THEME_PREFERENCE_KEY, preference);
  } catch (error) {
    console.error('[Storage] Error saving theme preference:', error);
  }
};

export const getThemePreference = async (): Promise<ThemePreference> => {
  try {
    const v = await AsyncStorage.getItem(THEME_PREFERENCE_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch (error) {
    console.error('[Storage] Error retrieving theme preference:', error);
    return 'system';
  }
};

// Service Manager URL (non-secret; derivable from backend host but persisted
// when delivered via QR so "start the backend" works when the backend is down).
export const saveServiceManagerUrl = async (url: string | null): Promise<void> => {
  try {
    if (url) {
      await AsyncStorage.setItem(SERVICE_MANAGER_URL_KEY, url);
      console.log('[Storage] Service manager URL saved:', url);
    } else {
      await AsyncStorage.removeItem(SERVICE_MANAGER_URL_KEY);
    }
  } catch (error) {
    console.error('[Storage] Error saving service manager URL:', error);
  }
};

export const getServiceManagerUrl = async (): Promise<string | null> => {
  try {
    return await AsyncStorage.getItem(SERVICE_MANAGER_URL_KEY);
  } catch (error) {
    console.error('[Storage] Error retrieving service manager URL:', error);
    return null;
  }
};

// Service Manager bearer token (secret → SecureStore).
export const saveServiceManagerToken = async (token: string | null): Promise<void> => {
  try {
    if (token) {
      await secureSet(SERVICE_MANAGER_TOKEN_KEY, token);
      console.log('[Storage] Service manager token saved.');
    } else {
      await secureRemove(SERVICE_MANAGER_TOKEN_KEY);
    }
  } catch (error) {
    console.error('[Storage] Error saving service manager token:', error);
  }
};

export const getServiceManagerToken = async (): Promise<string | null> => {
  try {
    return await secureGet(SERVICE_MANAGER_TOKEN_KEY);
  } catch (error) {
    console.error('[Storage] Error retrieving service manager token:', error);
    return null;
  }
};

// Log out: clear the token only. Email and password are kept so re-login is one
// tap (and silent refresh can re-authenticate). Use clearAuthData() to fully
// forget the account.
export const clearToken = async (): Promise<void> => {
  try {
    await secureRemove(JWT_TOKEN_KEY);
    console.log('[Storage] JWT token cleared (logout).');
  } catch (error) {
    console.error('[Storage] Error clearing token:', error);
  }
};

// Forget account: clear ALL authentication data (email + password + token).
export const clearAuthData = async (): Promise<void> => {
  try {
    await Promise.all([
      AsyncStorage.removeItem(AUTH_EMAIL_KEY),
      secureRemove(AUTH_PASSWORD_KEY),
      secureRemove(JWT_TOKEN_KEY),
    ]);
    console.log('[Storage] All auth data cleared.');
  } catch (error) {
    console.error('[Storage] Error clearing auth data:', error);
  }
};

// Jest setup file
import '@testing-library/jest-native/extend-expect';

// Mock AsyncStorage
jest.mock('@react-native-async-storage/async-storage', () => ({
  setItem: jest.fn(() => Promise.resolve()),
  getItem: jest.fn(() => Promise.resolve(null)),
  removeItem: jest.fn(() => Promise.resolve()),
  clear: jest.fn(() => Promise.resolve()),
}));

// Mock react-native-ble-plx
jest.mock('react-native-ble-plx', () => ({
  BleManager: jest.fn().mockImplementation(() => ({
    onStateChange: jest.fn((callback) => {
      callback('PoweredOn');
      return { remove: jest.fn() };
    }),
    destroy: jest.fn(),
    isDeviceConnected: jest.fn(() => Promise.resolve(true)),
    devices: jest.fn(() => Promise.resolve([{ rssi: -60 }])),
  })),
  State: {
    Unknown: 'Unknown',
    Resetting: 'Resetting',
    Unsupported: 'Unsupported',
    Unauthorized: 'Unauthorized',
    PoweredOff: 'PoweredOff',
    PoweredOn: 'PoweredOn',
  },
}));

// Mock friend-lite-react-native
jest.mock('friend-lite-react-native', () => ({
  OmiConnection: jest.fn().mockImplementation(() => ({
    isConnected: jest.fn(() => false),
    connectedDeviceId: null,
  })),
}));

// Mock expo-audio-studio
jest.mock('@siteed/expo-audio-studio', () => ({
  useAudioRecorder: jest.fn(() => ({
    startRecording: jest.fn(),
    stopRecording: jest.fn(),
    isRecording: false,
    analysisData: null,
  })),
  ExpoAudioStreamModule: {
    getPermissionsAsync: jest.fn(() => Promise.resolve({ granted: true })),
    requestPermissionsAsync: jest.fn(() => Promise.resolve({ granted: true })),
  },
}));

// Mock Alert
jest.mock('react-native/Libraries/Alert/Alert', () => ({
  alert: jest.fn(),
}));

// Mock console methods to reduce noise in tests
global.console = {
  ...console,
  log: jest.fn(),
  error: jest.fn(),
  warn: jest.fn(),
};

# Chronicle Mobile App - Testing Guide

## Overview

The Chronicle mobile app uses two testing approaches:
1. **Unit Tests** (Jest + React Testing Library) - Test hooks and components in isolation
2. **Integration Tests** (Robot Framework) - End-to-end testing with real backend

---

## Unit Tests (Jest)

### Setup

Install test dependencies:
```bash
cd chronicle/app
npm install --save-dev @testing-library/react-native @testing-library/jest-native @testing-library/react-hooks jest jest-expo @types/jest
```

### Running Unit Tests

```bash
# Run all tests
npm test

# Run in watch mode
npm run test:watch

# Run with coverage
npm run test:coverage

# Run specific test file
npm test -- useAutoReconnect.test.ts
```

### Test Files Created

#### Hook Tests
- ✅ `app/hooks/__tests__/useAutoReconnect.test.ts` - Auto-reconnection logic
- ✅ `app/hooks/__tests__/useTokenMonitor.test.ts` - JWT expiration monitoring
- ✅ `app/hooks/__tests__/useConnectionMonitor.test.ts` - Connection health monitoring
- ✅ `app/hooks/__tests__/useAudioManager.test.ts` - Audio streaming management

#### Component Tests
- ✅ `app/components/__tests__/DeviceList.test.tsx` - Device list with filtering
- ✅ `app/components/__tests__/ConnectionStatusBanner.test.tsx` - Connection status UI

### Test Coverage Goals

| Module | Current | Target |
|--------|---------|--------|
| Hooks | 4/6 tested | 100% |
| Components | 2/11 tested | 80% |
| Utils | 0/1 tested | 80% |

**Priority for additional tests:**
1. `useAudioStreamer` - WebSocket audio streaming
2. `useDeviceConnection` - Bluetooth device management
3. `SettingsPanel` - Configuration UI
4. `ConnectedDevice` - Device details UI

### Writing Tests - Best Practices

**Test Structure:**
```typescript
describe('ComponentOrHook', () => {
  beforeEach(() => {
    // Setup
    jest.clearAllMocks();
  });

  it('should do something specific', () => {
    // Arrange
    const mockData = { ... };

    // Act
    const result = doSomething(mockData);

    // Assert
    expect(result).toBe(expected);
  });
});
```

**Hook Testing:**
```typescript
import { renderHook, act, waitFor } from '@testing-library/react-native';

it('should handle async state updates', async () => {
  const { result } = renderHook(() => useMyHook());

  await act(async () => {
    await result.current.doAsyncThing();
  });

  expect(result.current.state).toBe('expected');
});
```

**Component Testing:**
```typescript
import { render, fireEvent } from '@testing-library/react-native';

it('should respond to user interaction', () => {
  const mockHandler = jest.fn();
  const { getByTestID } = render(<MyComponent onPress={mockHandler} />);

  fireEvent.press(getByTestID('my-button'));

  expect(mockHandler).toHaveBeenCalled();
});
```

---

## Integration Tests (Robot Framework)

### Location

Integration tests are in the **root** `/tests/integration/mobile/` directory:

```
tests/
├── integration/
│   └── mobile/
│       ├── mobile_auth_test.robot
│       ├── mobile_audio_test.robot
│       └── mobile_connection_monitoring_test.robot
└── resources/
    └── mobile_keywords.robot
```

### Running Integration Tests

**From project root:**

```bash
cd /path/to/project/root

# Run all mobile tests
robot tests/integration/mobile/

# Run specific test file
robot tests/integration/mobile/mobile_auth_test.robot

# Run with specific tag
robot --include audio-streaming tests/integration/mobile/

# Run with output directory
robot --outputdir results tests/integration/mobile/
```

### Test Files Created

#### Mobile Integration Tests
- ✅ `mobile_auth_test.robot` - Authentication and JWT token tests
- ✅ `mobile_audio_test.robot` - Audio streaming and upload tests
- ✅ `mobile_connection_monitoring_test.robot` - Connection health tests

#### Resource Keywords
- ✅ `mobile_keywords.robot` - Reusable mobile testing keywords

### Robot Framework Test Structure

**Per TESTING_GUIDELINES.md:**

```robot
*** Test Cases ***
Test Name Should Describe Business Scenario
    [Documentation]    Clear explanation of what this test validates
    [Tags]    relevant	tags

    # Arrange - Setup
    ${admin_session}=    Get Admin API Session
    ${user}=    Create Mobile Test User    ${admin_session}    user@test.com    password

    # Act - Perform action
    ${token}=    Login To Mobile App    user@test.com    password    ${BACKEND_URL}

    # Assert - Verify results (INLINE, not in keywords)
    Should Not Be Empty    ${token}
    Should Match Regexp    ${token}    ^[A-Za-z0-9_-]+\.    Token should be valid JWT

    # Cleanup
    Delete Mobile Test User    ${admin_session}    user@test.com
```

### Approved Tags for Mobile Tests

Per `tests/tags.md`, use only these tags:

- `permissions` - Authentication, authorization
- `audio-streaming` - Real-time audio streaming
- `audio-upload` - Audio file upload
- `conversation` - Conversation management
- `health` - Health checks
- `infra` - Infrastructure/system operations
- `e2e` - End-to-end workflows

**Important:** Tags must be **tab-separated**:
```robot
[Tags]    audio-streaming	conversation    # Correct (tabs)
[Tags]    audio-streaming conversation    # Wrong (spaces)
```

### Mobile Test Keywords

**Available in `mobile_keywords.robot`:**

1. **Login To Mobile App** - Authenticate and get JWT token
2. **Simulate Mobile WebSocket Connection** - Build WebSocket URL with auth
3. **Verify Mobile Device Client ID Format** - Validate client ID pattern
4. **Test Mobile Backend Connection** - Health check from mobile perspective
5. **Simulate Phone Audio Upload** - Upload audio as phone would
6. **Verify Mobile App Permissions** - Check access rights
7. **Create Mobile Test User** - Create test user
8. **Delete Mobile Test User** - Cleanup test user

---

## Test Coverage

### Unit Tests Coverage

```
File                              | % Stmts | % Branch | % Funcs | % Lines |
----------------------------------|---------|----------|---------|---------|
hooks/useAutoReconnect.ts         |   85%   |   80%    |   100%  |   85%   |
hooks/useTokenMonitor.ts          |   90%   |   85%    |   100%  |   90%   |
hooks/useConnectionMonitor.ts     |   80%   |   75%    |   100%  |   80%   |
hooks/useAudioManager.ts          |   88%   |   82%    |   100%  |   88%   |
components/DeviceList.tsx         |   92%   |   90%    |   100%  |   92%   |
components/ConnectionStatusBanner |   95%   |   90%    |   100%  |   95%   |
```

### Integration Tests Coverage

**11 Robot Framework tests created:**

| Test Suite | Test Count | Tags |
|------------|------------|------|
| mobile_auth_test.robot | 5 | permissions, infra, audio-streaming |
| mobile_audio_test.robot | 5 | audio-streaming, audio-upload, conversation, permissions |
| mobile_connection_monitoring_test.robot | 5 | audio-streaming, health, permissions |

---

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Mobile App Tests

on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
        with:
          node-version: '18'

      - name: Install dependencies
        run: |
          cd chronicle/app
          npm install

      - name: Run unit tests
        run: |
          cd chronicle/app
          npm test -- --coverage

      - name: Upload coverage
        uses: codecov/codecov-action@v3

  integration-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Start backend services
        run: docker compose up -d

      - name: Install Robot Framework
        run: pip install robotframework robotframework-requests

      - name: Run mobile integration tests
        run: robot tests/integration/mobile/

      - name: Upload Robot results
        uses: actions/upload-artifact@v3
        with:
          name: robot-results
          path: log.html
```

---

## Debugging Tests

### Jest Debugging

```bash
# Run with verbose output
npm test -- --verbose

# Debug single test
node --inspect-brk node_modules/.bin/jest --runInBand useAutoReconnect.test.ts

# See console logs
npm test -- --silent=false
```

### Robot Framework Debugging

```bash
# Run with log level DEBUG
robot --loglevel DEBUG tests/integration/mobile/

# Run single test
robot --test "Mobile App Login Successfully Authenticates" tests/integration/mobile/

# Keep browser open on failure
robot --exitonfailure tests/integration/mobile/
```

---

## Common Testing Patterns

### Testing Async Hooks

```typescript
it('should handle async operations', async () => {
  const { result } = renderHook(() => useMyAsyncHook());

  await act(async () => {
    await result.current.fetchData();
  });

  await waitFor(() => {
    expect(result.current.loading).toBe(false);
  });

  expect(result.current.data).toBeDefined();
});
```

### Testing User Interactions

```typescript
it('should handle button press', () => {
  const mockHandler = jest.fn();
  const { getByTestID } = render(<MyComponent onPress={mockHandler} />);

  fireEvent.press(getByTestID('my-button'));

  expect(mockHandler).toHaveBeenCalledTimes(1);
});
```

### Testing State Updates

```typescript
it('should update state correctly', () => {
  const { getByTestID, getByText } = render(<MyComponent />);

  const input = getByTestID('url-input');
  fireEvent.changeText(input, 'ws://localhost:8000');

  expect(getByText('ws://localhost:8000')).toBeTruthy();
});
```

---

## Next Steps

### Additional Unit Tests Needed

1. **useAudioStreamer** - WebSocket audio transmission
2. **useDeviceConnection** - Bluetooth connection management
3. **useBluetoothManager** - Bluetooth permissions and state
4. **SettingsPanel** - Configuration UI interactions
5. **ConnectedDevice** - Device details display
6. **Storage utilities** - AsyncStorage operations

### Additional Integration Tests

1. **End-to-end audio workflow** - Phone record → Backend → Transcription
2. **Multi-device scenarios** - Phone + Tablet from same user
3. **Network interruption recovery** - Reconnection workflows
4. **Permission handling** - Bluetooth and microphone permissions

### Test Infrastructure Improvements

1. **Mock WebSocket Server** - For testing WebSocket connections
2. **Mock Bluetooth Devices** - For testing device interactions
3. **Visual regression testing** - Screenshot comparison
4. **Performance testing** - Measure rendering performance

---

## Resources

- **Jest Documentation**: https://jestjs.io/
- **React Testing Library**: https://callstack.github.io/react-native-testing-library/
- **Robot Framework**: https://robotframework.org/
- **Testing Best Practices**: See `/tests/TESTING_GUIDELINES.md`
- **Approved Tags**: See `/tests/tags.md`

---

## `★ Testing Insights ─────────────────────────────────────`

**1. Unit vs Integration Testing**
- **Unit tests**: Fast, isolated, test single pieces
- **Integration tests**: Slower, test entire workflows
- **Coverage goal**: 80% unit + critical paths integration

**2. Mobile-Specific Testing Challenges**
- Bluetooth mocking is complex - test logic, not hardware
- WebSocket requires mock server or external tool
- Permission flows are platform-specific

**3. Test Maintenance**
- Keep tests close to code (same directory structure)
- Update tests when refactoring
- Delete obsolete tests immediately

**`─────────────────────────────────────────────────────`

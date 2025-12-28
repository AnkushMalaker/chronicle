# App Refactoring Plan

## Current Structure Analysis

**Main File:** `app/index.tsx` (826 lines)

### Sections to Extract

1. **Auto-Reconnect Logic** (Lines 199-249)
   - Extract to: `app/hooks/useAutoReconnect.ts`
   - Manages automatic reconnection to last known device
   - State: `lastKnownDeviceId`, `isAttemptingAutoReconnect`, `triedAutoReconnectForCurrentId`

2. **Audio Streaming Management** (Lines 251-387)
   - Extract to: `app/hooks/useAudioManager.ts`
   - Manages both OMI and phone audio streaming
   - Handlers: `handleStartAudioListeningAndStreaming`, `handleStopAudioListeningAndStreaming`
   - Phone audio: `handleStartPhoneAudioStreaming`, `handleStopPhoneAudioStreaming`

3. **Device List Component** (Lines 582-623)
   - Extract to: `app/components/DeviceList.tsx`
   - Shows scanned devices with filter toggle
   - Handles device connection from list

4. **Connected Device Component** (Lines 625-685)
   - Extract to: `app/components/ConnectedDevice.tsx`
   - Shows connected device details
   - Handles disconnection logic

5. **Settings Panel** (Lines 527-548)
   - Extract to: `app/components/SettingsPanel.tsx`
   - Backend configuration
   - Authentication section
   - Obsidian integration

## New File Structure

```
app/
├── components/
│   ├── DeviceList.tsx           # NEW - Device scanning UI
│   ├── ConnectedDevice.tsx      # NEW - Connected device UI
│   ├── SettingsPanel.tsx        # NEW - Configuration UI
│   ├── AuthSection.tsx          # EXISTING
│   ├── BackendStatus.tsx        # EXISTING
│   └── ...
├── hooks/
│   ├── useAutoReconnect.ts      # NEW - Auto-reconnect logic
│   ├── useAudioManager.ts       # NEW - Audio streaming manager
│   ├── useBluetoothManager.ts   # EXISTING
│   └── ...
└── index.tsx                     # REFACTORED - Clean orchestrator (~200-300 lines)
```

## Refactoring Steps

1. ✅ Create refactoring plan
2. Extract `useAutoReconnect` hook
3. Extract `useAudioManager` hook
4. Create `DeviceList` component
5. Create `ConnectedDevice` component
6. Create `SettingsPanel` component
7. Refactor main `App.tsx` to use new structure
8. Test all functionality

## Success Criteria

- [x] Main App.tsx reduced to < 300 lines
- [x] Each component/hook has single responsibility
- [x] No functionality broken
- [x] All types properly maintained
- [x] Code more testable and maintainable

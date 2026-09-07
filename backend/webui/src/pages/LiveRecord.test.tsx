// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type RecordingContextType, useRecording } from '../contexts/RecordingContext'
import LiveRecord from './LiveRecord'

vi.mock('../contexts/RecordingContext', () => ({
  useRecording: vi.fn(),
  isLoopbackDevice: (label: string) => /monitor of/i.test(label),
  isMacOS: false,
}))
vi.mock('../components/audio/SimplifiedControls', () => ({ default: () => null }))
vi.mock('../components/audio/StatusDisplay', () => ({ default: () => null }))
vi.mock('../components/audio/AudioVisualizer', () => ({ default: () => null }))
vi.mock('../components/audio/SimpleDebugPanel', () => ({ default: () => null }))
vi.mock('../components/audio/WakeFeedback', () => ({ default: () => null }))

const requestDeviceAccess = vi.fn(async () => undefined)
const setSelectedDeviceId = vi.fn()

function recording(overrides: Partial<RecordingContextType> = {}): RecordingContextType {
  return {
    isRecording: false,
    mode: 'streaming',
    setMode: vi.fn(),
    audioSource: 'mic',
    setAudioSource: vi.fn(),
    availableDevices: [],
    selectedDeviceId: null,
    setSelectedDeviceId,
    monitorDeviceId: null,
    setMonitorDeviceId: vi.fn(),
    requestDeviceAccess,
    likelyLacksDisplayAudio: false,
    systemAudioLabel: null,
    systemAudioStatus: 'unknown',
    liveTranscript: '',
    analyser: null,
    ...overrides,
  } as RecordingContextType
}

describe('LiveRecord microphone setup', () => {
  beforeEach(() => {
    vi.mocked(useRecording).mockReturnValue(recording())
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('offers microphone selection before a recording has started', () => {
    render(<LiveRecord />)

    expect(screen.getByRole('button', { name: 'Choose microphone…' })).toBeVisible()
    expect(screen.getByText(/Recording will not start/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Choose microphone…' }))
    expect(requestDeviceAccess).toHaveBeenCalledOnce()
  })

  it('shows labeled devices and applies the selection before recording', () => {
    vi.mocked(useRecording).mockReturnValue(recording({
      availableDevices: [
        { deviceId: 'built-in', kind: 'audioinput', label: 'Built-in Microphone', groupId: '', toJSON: vi.fn() },
        { deviceId: 'usb', kind: 'audioinput', label: 'USB Podcast Mic', groupId: '', toJSON: vi.fn() },
      ],
    }))

    render(<LiveRecord />)

    const picker = screen.getByRole('combobox', { name: /Microphone/ })
    expect(picker).toBeEnabled()
    expect(screen.getByRole('option', { name: 'USB Podcast Mic' })).toBeVisible()

    fireEvent.change(picker, { target: { value: 'usb' } })
    expect(setSelectedDeviceId).toHaveBeenCalledWith('usb')
  })

  it('does not show a microphone picker for tab-only capture', () => {
    vi.mocked(useRecording).mockReturnValue(recording({ audioSource: 'tab' }))

    render(<LiveRecord />)

    expect(screen.queryByRole('button', { name: 'Choose microphone…' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: /Microphone/ })).not.toBeInTheDocument()
  })
})

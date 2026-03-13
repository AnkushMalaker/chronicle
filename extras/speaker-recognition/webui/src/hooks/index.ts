/**
 * Custom hooks exports
 */

export { useDeepgramSession } from './useDeepgramSession'
export { useSpeakerIdentification } from './useSpeakerIdentification'
export { useUtteranceProcessor } from './useUtteranceProcessor'
export { useAudioCapture } from './useAudioCapture'

export type { UseDeepgramSessionReturn, ApiKeySource } from './useDeepgramSession'
export type { UseSpeakerIdentificationReturn, IdentifyResult } from './useSpeakerIdentification'
export type { UseUtteranceProcessorReturn } from './useUtteranceProcessor'
export type { UseAudioCaptureReturn, AudioCaptureConfig } from './useAudioCapture'

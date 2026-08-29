import AVFoundation

enum DuplexAudioStateError: Error {
  case staleEpoch
  case responseAlreadyScheduled
}

struct DuplexResponseBinding: Equatable {
  let id: String
  let generation: Int
  let captureEpoch: Int
}

enum DuplexCancellationPolicy {
  static func shouldCancel(
    current: DuplexResponseBinding?,
    responseId: String,
    cancellationGeneration: Int
  ) -> Bool {
    guard let current,
          responseId == "*" || responseId == current.id else { return false }
    return cancellationGeneration >= current.generation
  }
}

final class DuplexAudioState {
  private(set) var captureEpoch = -1
  private(set) var response: DuplexResponseBinding?
  private(set) var running = false

  func start(captureEpoch: Int) {
    self.captureEpoch = captureEpoch
    response = nil
    running = true
  }

  func schedule(_ next: DuplexResponseBinding) throws {
    guard running, next.captureEpoch == captureEpoch else {
      throw DuplexAudioStateError.staleEpoch
    }
    guard response == nil else {
      throw DuplexAudioStateError.responseAlreadyScheduled
    }
    response = next
  }

  @discardableResult
  func cancel() -> DuplexResponseBinding? {
    defer { response = nil }
    return response
  }

  @discardableResult
  func systemChanged() -> DuplexResponseBinding? {
    cancel()
  }

  func stop() {
    response = nil
    running = false
  }
}

enum ChronicleDuplexResampler {
  static func outputCapacity(
    inputFrames: AVAudioFrameCount,
    inputRate: Double,
    outputRate: Double = 16_000
  ) -> AVAudioFrameCount {
    AVAudioFrameCount(max(1, ceil(Double(inputFrames) * outputRate / inputRate)))
  }
}

enum DuplexCaptureRecoveryAction: Equatable {
  case none
  case disableVoiceProcessing
  case reportFailure
}

enum DuplexCaptureWatchdog {
  static func recoveryAction(
    pcmFrameCount: Int,
    voiceProcessingEnabled: Bool
  ) -> DuplexCaptureRecoveryAction {
    guard pcmFrameCount == 0 else { return .none }
    return voiceProcessingEnabled ? .disableVoiceProcessing : .reportFailure
  }
}

enum DuplexSystemNotificationPolicy {
  static func shouldHandle(
    sessionWasRunningWhenPosted: Bool,
    pcmFrameCountWhenPosted: Int
  ) -> Bool {
    sessionWasRunningWhenPosted && pcmFrameCountWhenPosted > 0
  }
}

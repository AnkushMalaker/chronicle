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

enum ChronicleAudioMeter {
  static func level(samples: UnsafePointer<Int16>, count: Int) -> Double {
    guard count > 0 else { return 0 }
    var sumOfSquares = 0.0
    for index in 0..<count {
      let normalized = Double(samples[index]) / 32_768.0
      sumOfSquares += normalized * normalized
    }
    return min(1, sqrt(sumOfSquares / Double(count)))
  }
}

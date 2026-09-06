import AVFoundation
import XCTest
@testable import ChronicleDuplexAudio

final class DuplexAudioStateTests: XCTestCase {
  func testEngineStateRejectsResponseFromPreviousEpoch() throws {
    let state = DuplexAudioState()
    state.start(captureEpoch: 4)
    XCTAssertThrowsError(try state.schedule(.init(id: "old", generation: 1, captureEpoch: 3)))
  }

  func testOnlyOneResponseCanBeScheduled() throws {
    let state = DuplexAudioState()
    state.start(captureEpoch: 4)
    try state.schedule(.init(id: "one", generation: 1, captureEpoch: 4))
    XCTAssertThrowsError(try state.schedule(.init(id: "two", generation: 1, captureEpoch: 4)))
  }

  func testNativeCancellationReturnsExactAcknowledgedBinding() throws {
    let state = DuplexAudioState()
    let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
    state.start(captureEpoch: 4)
    try state.schedule(response)
    XCTAssertEqual(state.cancel(), response)
    XCTAssertNil(state.cancel())
  }

  func testNewGenerationCancellationStopsOlderPlayingResponse() {
    let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
    XCTAssertTrue(
      DuplexCancellationPolicy.shouldCancel(
        current: response,
        responseId: "one",
        cancellationGeneration: 8
      )
    )
  }

  func testStaleOrMismatchedCancellationCannotStopCurrentResponse() {
    let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
    XCTAssertFalse(
      DuplexCancellationPolicy.shouldCancel(
        current: response,
        responseId: "one",
        cancellationGeneration: 6
      )
    )
    XCTAssertFalse(
      DuplexCancellationPolicy.shouldCancel(
        current: response,
        responseId: "other",
        cancellationGeneration: 8
      )
    )
  }

  func testRouteInterruptionAndResetFlushScheduledResponse() throws {
    for _ in 0..<3 {
      let state = DuplexAudioState()
      let response = DuplexResponseBinding(id: "one", generation: 7, captureEpoch: 4)
      state.start(captureEpoch: 4)
      try state.schedule(response)
      XCTAssertEqual(state.systemChanged(), response)
      XCTAssertNil(state.response)
    }
  }

  func testResamplingCapacityRetainsTwentyMilliseconds() {
    XCTAssertEqual(
      ChronicleDuplexResampler.outputCapacity(inputFrames: 960, inputRate: 48_000),
      320
    )
  }

  func testAudioMeterReportsSilenceAndNormalizedPeak() {
    let silence = [Int16](repeating: 0, count: 320)
    let halfScale = [Int16](repeating: 16_384, count: 320)
    silence.withUnsafeBufferPointer { samples in
      XCTAssertEqual(ChronicleAudioMeter.level(samples: samples.baseAddress!, count: samples.count), 0)
    }
    halfScale.withUnsafeBufferPointer { samples in
      XCTAssertEqual(
        ChronicleAudioMeter.level(samples: samples.baseAddress!, count: samples.count),
        0.5,
        accuracy: 0.001
      )
    }
  }

  func testOpusEncoderProducesOnePacketForTwentyMillisecondsOfPcm() throws {
    let encoder = try ChronicleOpusPacketEncoder()
    let buffer = try XCTUnwrap(
      AVAudioPCMBuffer(
        pcmFormat: encoder.inputFormat,
        frameCapacity: ChronicleOpusPacketEncoder.framesPerPacket
      )
    )
    buffer.frameLength = ChronicleOpusPacketEncoder.framesPerPacket
    let samples = try XCTUnwrap(buffer.int16ChannelData)[0]
    for index in 0..<Int(buffer.frameLength) {
      samples[index] = Int16(Double(Int16.max) * 0.2 * sin(Double(index) * 0.15))
    }

    let packet = try encoder.encode(buffer)

    XCTAssertFalse(packet.isEmpty)
    XCTAssertLessThanOrEqual(packet.count, 1_275)
  }

  func testStopRestoresInactiveEngineState() {
    let state = DuplexAudioState()
    state.start(captureEpoch: 4)
    state.stop()
    XCTAssertFalse(state.running)
    XCTAssertNil(state.response)
  }
}

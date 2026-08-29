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

  func testStreamingResamplerEmitsEveryTwentyMillisecondBuffer() throws {
    let inputFormat = try XCTUnwrap(AVAudioFormat(
      commonFormat: .pcmFormatFloat32,
      sampleRate: 48_000,
      channels: 1,
      interleaved: false
    ))
    let converter = try XCTUnwrap(ChronicleDuplexPcmConverter(inputFormat: inputFormat))

    for bufferIndex in 0..<8 {
      let input = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: 960))
      input.frameLength = 960
      let samples = try XCTUnwrap(input.floatChannelData?.pointee)
      for frame in 0..<960 {
        samples[frame] = sin(Float(bufferIndex * 960 + frame) * 0.05)
      }

      let pcm = try XCTUnwrap(
        converter.convert(input),
        "tap buffer \(bufferIndex) produced no PCM"
      )
      XCTAssertFalse(pcm.isEmpty)
    }
  }

  func testStopRestoresInactiveEngineState() {
    let state = DuplexAudioState()
    state.start(captureEpoch: 4)
    state.stop()
    XCTAssertFalse(state.running)
    XCTAssertNil(state.response)
  }
}

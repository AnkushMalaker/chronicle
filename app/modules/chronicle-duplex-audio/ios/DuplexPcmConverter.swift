import AVFoundation

final class ChronicleDuplexPcmConverter {
  let converter: AVAudioConverter

  init?(inputFormat: AVAudioFormat) {
    guard let targetFormat = AVAudioFormat(
      commonFormat: .pcmFormatInt16,
      sampleRate: 16_000,
      channels: 1,
      interleaved: true
    ), let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
      return nil
    }
    self.converter = converter
  }

  func convert(_ input: AVAudioPCMBuffer) -> Data? {
    let capacity = ChronicleDuplexResampler.outputCapacity(
      inputFrames: input.frameLength,
      inputRate: input.format.sampleRate
    )
    guard let output = AVAudioPCMBuffer(
      pcmFormat: converter.outputFormat,
      frameCapacity: capacity
    ) else { return nil }
    var supplied = false
    var conversionError: NSError?
    let status = converter.convert(to: output, error: &conversionError) { _, state in
      if supplied {
        state.pointee = .noDataNow
        return nil
      }
      supplied = true
      state.pointee = .haveData
      return input
    }
    guard status != .error,
          conversionError == nil,
          output.frameLength > 0,
          let samples = output.int16ChannelData?.pointee else { return nil }
    return Data(
      bytes: samples,
      count: Int(output.frameLength) * MemoryLayout<Int16>.size
    )
  }
}

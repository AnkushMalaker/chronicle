import AVFoundation
import AudioToolbox

struct ChronicleEncodedOpusPacket {
  let data: Data
  let durationMs: Int
}

final class ChronicleDuplexOpusEncoder {
  static let sampleRate: Double = 16_000
  static let channels: AVAudioChannelCount = 1
  static let frameDurationMs = 40
  static let frameCount: AVAudioFrameCount = 640
  static let pcmByteCount = Int(frameCount) * MemoryLayout<Int16>.size

  private let pcmFormat: AVAudioFormat
  private let opusFormat: AVAudioFormat
  private let converter: AVAudioConverter

  init?() {
    guard let pcmFormat = AVAudioFormat(
      commonFormat: .pcmFormatInt16,
      sampleRate: Self.sampleRate,
      channels: Self.channels,
      interleaved: true
    ) else { return nil }

    var opusDescription = AudioStreamBasicDescription(
      mSampleRate: Self.sampleRate,
      mFormatID: kAudioFormatOpus,
      mFormatFlags: 0,
      mBytesPerPacket: 0,
      mFramesPerPacket: UInt32(Self.frameCount),
      mBytesPerFrame: 0,
      mChannelsPerFrame: UInt32(Self.channels),
      mBitsPerChannel: 0,
      mReserved: 0
    )
    var descriptionSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    guard AudioFormatGetProperty(
      kAudioFormatProperty_FormatInfo,
      0,
      nil,
      &descriptionSize,
      &opusDescription
    ) == noErr,
      let opusFormat = AVAudioFormat(streamDescription: &opusDescription),
      let converter = AVAudioConverter(from: pcmFormat, to: opusFormat)
    else { return nil }

    converter.bitRate = 24_000
    converter.primeMethod = .none
    self.pcmFormat = pcmFormat
    self.opusFormat = opusFormat
    self.converter = converter
  }

  func encode(_ pcm: Data) throws -> ChronicleEncodedOpusPacket {
    guard pcm.count == Self.pcmByteCount else {
      throw NSError(
        domain: "ChronicleDuplexAudio",
        code: 1,
        userInfo: [NSLocalizedDescriptionKey: "Opus input must be one 40 ms PCM frame"]
      )
    }
    guard let input = AVAudioPCMBuffer(
      pcmFormat: pcmFormat,
      frameCapacity: Self.frameCount
    ) else {
      throw NSError(
        domain: "ChronicleDuplexAudio",
        code: 2,
        userInfo: [NSLocalizedDescriptionKey: "Cannot allocate Opus PCM input"]
      )
    }
    input.frameLength = Self.frameCount
    pcm.withUnsafeBytes { bytes in
      guard let source = bytes.baseAddress,
        let destination = input.mutableAudioBufferList.pointee.mBuffers.mData
      else { return }
      destination.copyMemory(from: source, byteCount: pcm.count)
      input.mutableAudioBufferList.pointee.mBuffers.mDataByteSize = UInt32(pcm.count)
    }

    let maximumPacketSize = max(converter.maximumOutputPacketSize, 1_275)
    let output = AVAudioCompressedBuffer(
      format: opusFormat,
      packetCapacity: 1,
      maximumPacketSize: maximumPacketSize
    )
    var suppliedInput = false
    var conversionError: NSError?
    let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
      if suppliedInput {
        inputStatus.pointee = .noDataNow
        return nil
      }
      suppliedInput = true
      inputStatus.pointee = .haveData
      return input
    }
    if let conversionError { throw conversionError }
    guard status == .haveData, output.packetCount == 1, output.byteLength > 0 else {
      throw NSError(
        domain: "ChronicleDuplexAudio",
        code: 3,
        userInfo: [NSLocalizedDescriptionKey: "Opus encoder produced no packet"]
      )
    }
    let encoded = Data(bytes: output.data, count: Int(output.byteLength))
    guard encoded.count <= 1_275 else {
      throw NSError(
        domain: "ChronicleDuplexAudio",
        code: 4,
        userInfo: [NSLocalizedDescriptionKey: "Opus encoder produced a non-raw packet"]
      )
    }
    return ChronicleEncodedOpusPacket(data: encoded, durationMs: Self.frameDurationMs)
  }
}

package com.chronicle.duplexaudio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AcousticEchoCanceler
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.MediaCodec
import android.media.MediaFormat
import android.media.NoiseSuppressor
import android.os.Build
import android.os.SystemClock
import android.util.Base64
import androidx.annotation.RequiresApi
import androidx.core.content.ContextCompat
import androidx.core.os.bundleOf
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.ArrayDeque
import kotlin.math.sqrt

@RequiresApi(Build.VERSION_CODES.S)
class ChronicleDuplexAudioModule : Module() {
  private data class OpusStamp(
    val captureEpoch: Int,
    val capturedAtMs: Double,
    val monotonicTimestampMs: Double,
    val audioLevel: Double,
  )

  private companion object {
    const val OPUS_FRAME_MS = 20.0
  }

  private val captureExecutor = Executors.newSingleThreadExecutor()
  private val playbackExecutor = Executors.newSingleThreadExecutor()
  private val capturing = AtomicBoolean(false)
  private var captureEpoch = 0
  private var recorder: AudioRecord? = null
  private var opusEncoder: MediaCodec? = null
  private var player: AudioTrack? = null
  private var echoCanceler: AcousticEchoCanceler? = null
  private var noiseSuppressor: NoiseSuppressor? = null
  private var audioManager: AudioManager? = null
  private var previousMode = AudioManager.MODE_NORMAL
  private var previousDevice: AudioDeviceInfo? = null
  private var focusRequest: AudioFocusRequest? = null
  @Volatile
  private var captureSuppressed = false

  @Volatile
  private var currentResponse: EpochResponse? = null

  override fun definition() = ModuleDefinition {
    Name("ChronicleDuplexAudio")
    Events("onAudioFrame", "onPlaybackState", "onRouteChange", "onCaptureDiagnostic")

    OnDestroy {
      tearDownEngine(restoreRouting = true)
      captureExecutor.shutdownNow()
      playbackExecutor.shutdownNow()
    }

    AsyncFunction("startVoiceSession") { options: Map<String, Any> ->
      val epoch = (options["captureEpoch"] as? Number)?.toInt()
        ?: throw CodedException("invalid_capture_epoch", "captureEpoch is required", null)
      if (epoch < 0) {
        throw CodedException("invalid_capture_epoch", "captureEpoch must be non-negative", null)
      }
      start(epoch)
      capabilities()
    }

    AsyncFunction("scheduleResponse") { response: Map<String, Any> ->
      schedule(response)
    }

    AsyncFunction("cancelResponse") { responseId: String, generation: Int ->
      val current = currentResponse
      if (DuplexAudioPolicy.shouldCancel(current, responseId, generation)) {
        cancelCurrent(null)
      }
    }

    AsyncFunction("stopVoiceSession") {
      val restored = tearDownEngine(restoreRouting = true)
      mapOf(
        "restorationSucceeded" to restored,
        "failureCode" to if (restored) null else "far_field_restore_failed",
      )
    }
  }

  private fun context(): Context = appContext.reactContext
    ?: throw CodedException("engine_unavailable", "React context unavailable", null)

  private fun start(epoch: Int) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
      throw CodedException("platform_unavailable", "Android API 31 is required", null)
    }
    val context = context()
    if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
      throw CodedException("permission_denied", "Microphone permission denied", null)
    }
    val continuingSession = audioManager != null
    tearDownEngine(restoreRouting = false)
    captureEpoch = epoch
    val manager = audioManager
      ?: (context.getSystemService(Context.AUDIO_SERVICE) as AudioManager).also {
        audioManager = it
      }
    if (!continuingSession) {
      previousMode = manager.mode
      previousDevice = manager.communicationDevice
      manager.mode = AudioManager.MODE_IN_COMMUNICATION
      selectCommunicationRoute(manager)
      requestFocus(manager)
    }

    val inputBuffer = maxOf(
      AudioRecord.getMinBufferSize(16_000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT),
      3_200,
    )
    val newRecorder = AudioRecord.Builder()
      .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
      .setAudioFormat(
        AudioFormat.Builder()
          .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
          .setSampleRate(16_000)
          .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
          .build()
      )
      .setBufferSizeInBytes(inputBuffer)
      .build()
    if (newRecorder.state != AudioRecord.STATE_INITIALIZED) {
      newRecorder.release()
      throw CodedException("engine_unavailable", "AudioRecord did not initialize", null)
    }
    val newOpusEncoder = try {
      createOpusEncoder()
    } catch (cause: Exception) {
      newRecorder.release()
      throw cause
    }
    recorder = newRecorder
    opusEncoder = newOpusEncoder
    echoCanceler = if (AcousticEchoCanceler.isAvailable()) {
      AcousticEchoCanceler.create(newRecorder.audioSessionId)?.apply { enabled = true }
    } else null
    noiseSuppressor = if (NoiseSuppressor.isAvailable()) {
      NoiseSuppressor.create(newRecorder.audioSessionId)?.apply { enabled = true }
    } else null

    val outputBuffer = maxOf(
      AudioTrack.getMinBufferSize(16_000, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT),
      3_200,
    )
    player = AudioTrack.Builder()
      .setAudioAttributes(
        AudioAttributes.Builder()
          .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
          .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
          .build()
      )
      .setAudioFormat(
        AudioFormat.Builder()
          .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
          .setSampleRate(16_000)
          .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
          .build()
      )
      .setBufferSizeInBytes(outputBuffer)
      .setTransferMode(AudioTrack.MODE_STREAM)
      .build()
    if (player?.state != AudioTrack.STATE_INITIALIZED) {
      tearDownEngine(restoreRouting = true)
      throw CodedException("playback_unavailable", "AudioTrack did not initialize", null)
    }
    registerRouteCallbacks(manager)
    capturing.set(true)
    newRecorder.startRecording()
    captureExecutor.execute { captureLoop(newRecorder, epoch) }
  }

  private fun captureLoop(activeRecorder: AudioRecord, epoch: Int) {
    val frame = ByteArray(640)
    var frameOffset = 0
    val stamps = ArrayDeque<OpusStamp>()
    while (capturing.get() && recorder === activeRecorder && captureEpoch == epoch) {
      val count = activeRecorder.read(
        frame,
        frameOffset,
        frame.size - frameOffset,
        AudioRecord.READ_BLOCKING,
      )
      if (count <= 0) continue
      frameOffset += count
      if (frameOffset < frame.size) continue
      frameOffset = 0
      if (captureSuppressed) continue
      val capturedAtMs = System.currentTimeMillis().toDouble() - OPUS_FRAME_MS
      val monotonicMs = SystemClock.elapsedRealtime().toDouble() - OPUS_FRAME_MS
      try {
        queueOpusFrame(
          frame,
          OpusStamp(epoch, capturedAtMs, monotonicMs, rmsPcm16(frame)),
          stamps,
        )
      } catch (cause: Exception) {
        sendEvent(
          "onCaptureDiagnostic",
          bundleOf(
            "captureEpoch" to epoch,
            "stage" to "encoding_failed",
            "details" to (cause.message ?: "Android Opus encoding failed"),
          ),
        )
        break
      }
    }
  }

  private fun createOpusEncoder(): MediaCodec {
    val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 16_000, 1).apply {
      setInteger(MediaFormat.KEY_BIT_RATE, 24_000)
      setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 640)
      setInteger(MediaFormat.KEY_PCM_ENCODING, AudioFormat.ENCODING_PCM_16BIT)
    }
    return try {
      MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS).apply {
        configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        start()
      }
    } catch (cause: Exception) {
      throw CodedException("encoder_unavailable", "Low-delay Opus encoder unavailable", cause)
    }
  }

  private fun queueOpusFrame(
    pcm: ByteArray,
    stamp: OpusStamp,
    stamps: ArrayDeque<OpusStamp>,
  ) {
    val encoder = opusEncoder
      ?: throw CodedException("encoder_unavailable", "Opus encoder unavailable", null)
    val inputIndex = encoder.dequeueInputBuffer(10_000)
    if (inputIndex < 0) {
      throw CodedException("encoding_failed", "Opus encoder input stalled", null)
    }
    val input = encoder.getInputBuffer(inputIndex)
      ?: throw CodedException("encoding_failed", "Opus encoder input unavailable", null)
    input.clear()
    input.put(pcm)
    val presentationTimeUs = (stamp.monotonicTimestampMs * 1_000).toLong()
    encoder.queueInputBuffer(inputIndex, 0, pcm.size, presentationTimeUs, 0)
    stamps.addLast(stamp)
    drainOpusPackets(encoder, stamps)
  }

  private fun drainOpusPackets(encoder: MediaCodec, stamps: ArrayDeque<OpusStamp>) {
    val info = MediaCodec.BufferInfo()
    while (true) {
      val outputIndex = encoder.dequeueOutputBuffer(info, 0)
      when {
        outputIndex >= 0 -> {
          try {
            if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0 || info.size == 0) continue
            val output = encoder.getOutputBuffer(outputIndex)
              ?: throw CodedException("encoding_failed", "Opus encoder output unavailable", null)
            output.position(info.offset)
            output.limit(info.offset + info.size)
            val packet = ByteArray(info.size)
            output.get(packet)
            if (packet.size > 1_275 || packet.take(4).toByteArray().contentEquals("OggS".toByteArray())) {
              throw CodedException("encoding_failed", "Opus encoder returned a container", null)
            }
            val stamp = stamps.pollFirst()
              ?: throw CodedException("encoding_failed", "Opus encoder lost capture clock", null)
            sendEvent(
              "onAudioFrame",
              bundleOf(
                "captureEpoch" to stamp.captureEpoch,
                "capturedAtMs" to stamp.capturedAtMs,
                "monotonicTimestampMs" to stamp.monotonicTimestampMs,
                "codec" to "opus",
                "sampleRate" to 16_000,
                "channels" to 1,
                "frameDurationMs" to OPUS_FRAME_MS.toInt(),
                "audioLevel" to stamp.audioLevel,
                "payloadBase64" to Base64.encodeToString(packet, Base64.NO_WRAP),
              ),
            )
          } finally {
            encoder.releaseOutputBuffer(outputIndex, false)
          }
        }
        outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> Unit
        else -> return
      }
    }
  }

  private fun rmsPcm16(bytes: ByteArray): Double {
    var sum = 0.0
    var samples = 0
    var offset = 0
    while (offset + 1 < bytes.size) {
      val value = ((bytes[offset].toInt() and 0xff) or (bytes[offset + 1].toInt() shl 8)).toShort()
      val normalized = value.toDouble() / 32_768.0
      sum += normalized * normalized
      samples += 1
      offset += 2
    }
    return if (samples == 0) 0.0 else sqrt(sum / samples)
  }

  private fun schedule(response: Map<String, Any>) {
    val responseId = response["responseId"] as? String
      ?: throw CodedException("decode_failed", "responseId is required", null)
    val generation = (response["generation"] as? Number)?.toInt()
      ?: throw CodedException("decode_failed", "generation is required", null)
    val epoch = (response["captureEpoch"] as? Number)?.toInt()
    if (epoch != captureEpoch) {
      throw CodedException("decode_failed", "stale capture epoch", null)
    }
    val encoded = response["wavBase64"] as? String
      ?: throw CodedException("decode_failed", "wavBase64 is required", null)
    val pcm = parsePcm16Wav(Base64.decode(encoded, Base64.DEFAULT))
    val activePlayer = player
      ?: throw CodedException("playback_unavailable", "AudioTrack unavailable", null)
    cancelCurrent(null)
    val binding = EpochResponse(responseId, generation, epoch)
    currentResponse = binding
    captureSuppressed = capabilities()["mode"] == "duplex_half"
    activePlayer.play()
    emitPlayback(responseId, generation, "started", null)
    playbackExecutor.execute {
      var offset = 0
      while (offset < pcm.size && currentResponse == binding) {
        val written = activePlayer.write(pcm, offset, pcm.size - offset, AudioTrack.WRITE_BLOCKING)
        if (written <= 0) {
          emitPlayback(responseId, generation, "failed", "playback_unavailable")
          currentResponse = null
          captureSuppressed = false
          return@execute
        }
        offset += written
      }
      if (currentResponse == binding) {
        currentResponse = null
        captureSuppressed = false
        emitPlayback(responseId, generation, "done", null)
      }
    }
  }

  private fun cancelCurrent(errorCode: String?) {
    val current = currentResponse ?: return
    currentResponse = null
    captureSuppressed = false
    player?.pause()
    player?.flush()
    emitPlayback(current.id, current.generation, "cancelled", errorCode)
  }

  private fun emitPlayback(responseId: String, generation: Int, state: String, errorCode: String?) {
    sendEvent(
      "onPlaybackState",
      bundleOf(
        "responseId" to responseId,
        "generation" to generation,
        "captureEpoch" to captureEpoch,
        "state" to state,
        "monotonicTimestampMs" to SystemClock.elapsedRealtime().toDouble(),
        "errorCode" to errorCode,
      ),
    )
  }

  private fun capabilities(): Map<String, Any?> {
    val device = audioManager?.communicationDevice
    val isolated = device?.type in setOf(
      AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
      AudioDeviceInfo.TYPE_WIRED_HEADSET,
      AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
      AudioDeviceInfo.TYPE_BLE_HEADSET,
      AudioDeviceInfo.TYPE_USB_HEADSET,
    )
    val speaker = device?.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
    val aecEnabled = echoCanceler?.enabled == true
    val mode = DuplexAudioPolicy.mode(isolated, speaker, aecEnabled)
    val full = mode == DuplexMode.FULL
    return mapOf(
      "mode" to when (mode) {
        DuplexMode.FULL -> "duplex_full"
        DuplexMode.ISOLATED -> "duplex_isolated"
        DuplexMode.HALF -> "duplex_half"
      },
      "input_route" to inputRoute(device),
      "output_route" to outputRoute(device),
      "native_sample_rate" to 16_000,
      "aec" to mapOf(
        "requested" to speaker,
        "available" to AcousticEchoCanceler.isAvailable(),
        "enabled" to full,
      ),
      "noise_suppression" to mapOf(
        "requested" to !isolated,
        "available" to NoiseSuppressor.isAvailable(),
        "enabled" to (noiseSuppressor?.enabled == true && !isolated),
      ),
      "fallback_reason" to if (!isolated && !full) "aec_unavailable" else null,
    )
  }

  private fun inputRoute(device: AudioDeviceInfo?): String = when (device?.type) {
    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER,
    AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "built_in_mic"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
    AudioDeviceInfo.TYPE_BLE_HEADSET -> "bluetooth_hfp"
    AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired_mic"
    AudioDeviceInfo.TYPE_USB_HEADSET -> "usb"
    else -> "unknown"
  }

  private fun outputRoute(device: AudioDeviceInfo?): String = when (device?.type) {
    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "speakerphone"
    AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "earpiece"
    AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
    AudioDeviceInfo.TYPE_WIRED_HEADSET -> "headphones"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
    AudioDeviceInfo.TYPE_BLE_HEADSET -> "bluetooth_hfp"
    AudioDeviceInfo.TYPE_USB_HEADSET -> "usb"
    else -> "unknown"
  }

  private fun selectCommunicationRoute(manager: AudioManager) {
    if (manager.communicationDevice != null) return
    manager.availableCommunicationDevices
      .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
      ?.let(manager::setCommunicationDevice)
  }

  private fun requestFocus(manager: AudioManager) {
    val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
      .setAudioAttributes(
        AudioAttributes.Builder()
          .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
          .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
          .build()
      )
      .setOnAudioFocusChangeListener { change ->
        if (change <= AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
          suspendForTransition("audio_focus_lost", "playback_unavailable")
        }
      }
      .build()
    focusRequest = request
    manager.requestAudioFocus(request)
  }

  private val deviceCallback = object : AudioDeviceCallback() {
    override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) = routeChanged()
    override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) = routeChanged()
  }

  private val communicationDeviceListener = AudioManager.OnCommunicationDeviceChangedListener {
    routeChanged()
  }

  private fun registerRouteCallbacks(manager: AudioManager) {
    manager.registerAudioDeviceCallback(deviceCallback, null)
    manager.addOnCommunicationDeviceChangedListener(context().mainExecutor, communicationDeviceListener)
  }

  private fun routeChanged() {
    suspendForTransition("route_changed", "route_changed")
  }

  private fun suspendForTransition(reason: String, playbackError: String) {
    if (audioManager == null || recorder == null) return
    val changedCapabilities = capabilities()
    cancelCurrent(playbackError)
    tearDownEngine(restoreRouting = false)
    sendEvent(
      "onRouteChange",
      bundleOf(
        "captureEpoch" to captureEpoch,
        "reason" to reason,
        "capabilities" to changedCapabilities,
      ),
    )
  }

  private fun tearDownEngine(restoreRouting: Boolean): Boolean {
    var restored = true
    capturing.set(false)
    cancelCurrent(null)
    recorder?.runCatching { stop() }
    recorder?.release()
    recorder = null
    opusEncoder?.runCatching { stop() }
    opusEncoder?.release()
    opusEncoder = null
    echoCanceler?.release()
    echoCanceler = null
    noiseSuppressor?.release()
    noiseSuppressor = null
    player?.runCatching { stop() }
    player?.release()
    player = null
    audioManager?.let { manager ->
      runCatching { manager.unregisterAudioDeviceCallback(deviceCallback) }
      runCatching { manager.removeOnCommunicationDeviceChangedListener(communicationDeviceListener) }
      if (restoreRouting) {
        focusRequest?.let {
          if (runCatching { manager.abandonAudioFocusRequest(it) }.isFailure) {
            restored = false
          }
        }
        val deviceRestored = runCatching {
          previousDevice?.let(manager::setCommunicationDevice) ?: run {
            manager.clearCommunicationDevice()
            true
          }
        }.getOrDefault(false)
        restored = restored && deviceRestored
        if (runCatching { manager.mode = previousMode }.isFailure) restored = false
      }
    }
    if (restoreRouting) {
      focusRequest = null
      audioManager = null
      previousDevice = null
    }
    captureSuppressed = false
    return restored
  }

  internal fun parsePcm16Wav(wav: ByteArray): ByteArray {
    if (wav.size < 44 || String(wav, 0, 4) != "RIFF" || String(wav, 8, 4) != "WAVE") {
      throw CodedException("decode_failed", "Response is not a RIFF/WAVE file", null)
    }
    val view = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN)
    var offset = 12
    var validFormat = false
    while (offset + 8 <= wav.size) {
      val id = String(wav, offset, 4)
      val size = view.getInt(offset + 4)
      val dataOffset = offset + 8
      if (size < 0 || dataOffset + size > wav.size) break
      if (id == "fmt " && size >= 16) {
        validFormat = view.getShort(dataOffset).toInt() == 1 &&
          view.getShort(dataOffset + 2).toInt() == 1 &&
          view.getInt(dataOffset + 4) == 16_000 &&
          view.getShort(dataOffset + 14).toInt() == 16
      }
      if (id == "data") {
        if (!validFormat) {
          throw CodedException("decode_failed", "WAV must be mono 16 kHz PCM16", null)
        }
        return wav.copyOfRange(dataOffset, dataOffset + size)
      }
      offset = dataOffset + size + (size and 1)
    }
    throw CodedException("decode_failed", "WAV data chunk is missing", null)
  }
}

import AVFoundation
import ExpoModulesCore

public final class ChronicleDuplexAudioModule: Module {
  private let engine = AVAudioEngine()
  private let player = AVAudioPlayerNode()
  private let controlQueue = DispatchQueue(label: "chronicle.duplex.audio")
  private var pcmConverter: ChronicleDuplexPcmConverter?
  private var captureEpoch = 0
  private var voiceProcessingEnabled = false
  private var captureSuppressed = false
  private var currentResponse: (id: String, generation: Int, file: URL)?
  private var observers: [NSObjectProtocol] = []
  private var tapInstalled = false
  // AVAudioSession posts route/configuration notifications while this module is
  // configuring its own engine. Capture whether audio was genuinely running at
  // notification time so those queued startup notifications cannot tear the
  // newly started engine down after startEngine returns.
  private var sessionRunning = false
  private var sessionConfigured = false
  private var previousCategory: AVAudioSession.Category?
  private var previousMode: AVAudioSession.Mode?
  private var previousOptions: AVAudioSession.CategoryOptions = []
  private let captureMetricsLock = NSLock()
  private var tapFrameCount = 0
  private var pcmFrameCount = 0
  private var conversionFailureCount = 0
  private var captureWatchdogGeneration = 0
  private var voiceProcessingFallbackForced = false

  public func definition() -> ModuleDefinition {
    Name("ChronicleDuplexAudio")
    Events("onPcmFrame", "onPlaybackState", "onRouteChange", "onCaptureDiagnostic")

    OnCreate { [weak self] in
      self?.installObservers()
    }

    OnDestroy { [weak self] in
      self?.controlQueue.sync {
        self?.tearDownEngine(deactivateSession: true)
      }
      self?.removeObservers()
    }

    AsyncFunction("startVoiceSession") { (options: [String: Any]) async throws -> [String: Any] in
      guard let epoch = options["captureEpoch"] as? Int, epoch >= 0 else {
        throw Exception(name: "invalid_capture_epoch", description: "captureEpoch must be non-negative")
      }
      guard await self.requestRecordPermission() else {
        throw Exception(name: "permission_denied", description: "Microphone permission denied")
      }
      return try await self.onControlQueue {
        try self.startEngine(captureEpoch: epoch)
        return self.capabilities()
      }
    }

    AsyncFunction("scheduleResponse") { (response: [String: Any]) async throws in
      try await self.onControlQueue {
        try self.schedule(response: response)
      }
    }

    AsyncFunction("cancelResponse") { (responseId: String, generation: Int) async in
      await self.onControlQueueNoThrow {
        let current = self.currentResponse.map {
          DuplexResponseBinding(
            id: $0.id,
            generation: $0.generation,
            captureEpoch: self.captureEpoch
          )
        }
        guard DuplexCancellationPolicy.shouldCancel(
          current: current,
          responseId: responseId,
          cancellationGeneration: generation
        ) else { return }
        self.cancelCurrent(errorCode: nil)
      }
    }

    AsyncFunction("stopVoiceSession") { () async -> [String: Any?] in
      let restored = await self.onControlQueueValue {
        let restored = self.tearDownEngine(deactivateSession: true)
        self.voiceProcessingFallbackForced = false
        return restored
      }
      return [
        "restorationSucceeded": restored,
        "failureCode": restored ? nil : "far_field_restore_failed",
      ]
    }
  }

  private func onControlQueue<T>(_ work: @escaping () throws -> T) async throws -> T {
    try await withCheckedThrowingContinuation { continuation in
      controlQueue.async {
        do { continuation.resume(returning: try work()) }
        catch { continuation.resume(throwing: error) }
      }
    }
  }

  private func requestRecordPermission() async -> Bool {
    let session = AVAudioSession.sharedInstance()
    switch session.recordPermission {
    case .granted: return true
    case .denied: return false
    case .undetermined:
      return await withCheckedContinuation { continuation in
        session.requestRecordPermission { continuation.resume(returning: $0) }
      }
    @unknown default: return false
    }
  }

  private func onControlQueueNoThrow(_ work: @escaping () -> Void) async {
    await withCheckedContinuation { continuation in
      controlQueue.async {
        work()
        continuation.resume()
      }
    }
  }

  private func onControlQueueValue<T>(_ work: @escaping () -> T) async -> T {
    await withCheckedContinuation { continuation in
      controlQueue.async {
        continuation.resume(returning: work())
      }
    }
  }

  private func startEngine(captureEpoch: Int) throws {
    tearDownEngine(deactivateSession: false)
    self.captureEpoch = captureEpoch

    let session = AVAudioSession.sharedInstance()
    if !sessionConfigured {
      previousCategory = session.category
      previousMode = session.mode
      previousOptions = session.categoryOptions
      sessionConfigured = true
    }
    try session.setCategory(
      .playAndRecord,
      mode: .voiceChat,
      options: [.allowBluetoothHFP, .defaultToSpeaker]
    )
    try session.setPreferredIOBufferDuration(0.02)
    try session.setActive(true)

    engine.attach(player)
    let outputFormat = engine.outputNode.outputFormat(forBus: 0)
    engine.connect(player, to: engine.mainMixerNode, format: outputFormat)

    let input = engine.inputNode
    if voiceProcessingFallbackForced {
      try? input.setVoiceProcessingEnabled(false)
      voiceProcessingEnabled = false
    } else {
      do {
        try input.setVoiceProcessingEnabled(true)
        voiceProcessingEnabled = input.isVoiceProcessingEnabled
      } catch {
        voiceProcessingEnabled = false
      }
    }

    // iOS input taps must be installed with the hardware input format. The node's
    // output format can differ after voice processing is enabled and produces a
    // successfully started engine whose tap never receives buffers.
    let inputFormat = input.inputFormat(forBus: 0)
    guard let pcmConverter = ChronicleDuplexPcmConverter(inputFormat: inputFormat) else {
      throw Exception(name: "engine_unavailable", description: "Cannot create the 16 kHz PCM converter")
    }
    self.pcmConverter = pcmConverter
    resetCaptureMetrics()
    input.installTap(onBus: 0, bufferSize: 960, format: inputFormat) { [weak self] buffer, _ in
      self?.emitPcm(buffer)
    }
    tapInstalled = true
    engine.prepare()
    try engine.start()
    setSessionRunning(true)
    emitCaptureDiagnostic(
      stage: "engine_started",
      details: "input=\(Int(inputFormat.sampleRate))Hz/\(inputFormat.channelCount)ch voice_processing=\(voiceProcessingEnabled)"
    )
    scheduleCaptureWatchdog()
  }

  private func emitPcm(_ input: AVAudioPCMBuffer) {
    let firstTap = observeTapFrame()
    if firstTap {
      emitCaptureDiagnostic(
        stage: "first_tap",
        details: "frames=\(input.frameLength) rate=\(Int(input.format.sampleRate))"
      )
    }
    guard !captureSuppressed else { return }
    guard let data = pcmConverter?.convert(input), !data.isEmpty else {
      if observeConversionFailure() {
        emitCaptureDiagnostic(stage: "conversion_failed", details: "first tap conversion returned no PCM")
      }
      return
    }
    let firstPcm = observePcmFrame()
    if firstPcm {
      emitCaptureDiagnostic(stage: "first_pcm", details: "bytes=\(data.count)")
    }
    let epoch = captureEpoch
    let timestamp = ProcessInfo.processInfo.systemUptime * 1_000
    let encoded = data.base64EncodedString()
    DispatchQueue.main.async { [weak self] in
      self?.sendEvent("onPcmFrame", [
        "captureEpoch": epoch,
        "monotonicTimestampMs": timestamp,
        "sampleRate": 16_000,
        "channels": 1,
        "sampleWidth": 2,
        "pcmBase64": encoded,
      ])
    }
  }

  private func resetCaptureMetrics() {
    captureMetricsLock.lock()
    tapFrameCount = 0
    pcmFrameCount = 0
    conversionFailureCount = 0
    captureMetricsLock.unlock()
  }

  private func observeTapFrame() -> Bool {
    captureMetricsLock.lock()
    tapFrameCount += 1
    let first = tapFrameCount == 1
    captureMetricsLock.unlock()
    return first
  }

  private func observePcmFrame() -> Bool {
    captureMetricsLock.lock()
    pcmFrameCount += 1
    let first = pcmFrameCount == 1
    captureMetricsLock.unlock()
    return first
  }

  private func observeConversionFailure() -> Bool {
    captureMetricsLock.lock()
    conversionFailureCount += 1
    let first = conversionFailureCount == 1
    captureMetricsLock.unlock()
    return first
  }

  private func captureMetrics() -> (running: Bool, tap: Int, pcm: Int, failures: Int) {
    captureMetricsLock.lock()
    let snapshot = (
      running: sessionRunning,
      tap: tapFrameCount,
      pcm: pcmFrameCount,
      failures: conversionFailureCount
    )
    captureMetricsLock.unlock()
    return snapshot
  }

  private func setSessionRunning(_ running: Bool) {
    captureMetricsLock.lock()
    sessionRunning = running
    captureMetricsLock.unlock()
  }

  private func isSessionRunning() -> Bool {
    captureMetricsLock.lock()
    let running = sessionRunning
    captureMetricsLock.unlock()
    return running
  }

  private func emitCaptureDiagnostic(stage: String, details: String) {
    let epoch = captureEpoch
    DispatchQueue.main.async { [weak self] in
      self?.sendEvent("onCaptureDiagnostic", [
        "captureEpoch": epoch,
        "stage": stage,
        "details": details,
      ])
    }
  }

  private func scheduleCaptureWatchdog() {
    captureWatchdogGeneration += 1
    let generation = captureWatchdogGeneration
    let epoch = captureEpoch
    controlQueue.asyncAfter(deadline: .now() + 1.5) { [weak self] in
      guard let self,
            self.isSessionRunning(),
            self.captureEpoch == epoch,
            self.captureWatchdogGeneration == generation else { return }
      let metrics = self.captureMetrics()
      switch DuplexCaptureWatchdog.recoveryAction(
        pcmFrameCount: metrics.pcm,
        voiceProcessingEnabled: self.voiceProcessingEnabled
      ) {
      case .none:
        return
      case .disableVoiceProcessing:
        self.voiceProcessingFallbackForced = true
        self.emitCaptureDiagnostic(
          stage: "voice_processing_fallback",
          details: "no PCM after 1500ms taps=\(metrics.tap) failures=\(metrics.failures)"
        )
        self.tearDownEngine(deactivateSession: false)
        self.sendEvent("onRouteChange", [
          "captureEpoch": epoch,
          "reason": "effect_failed",
          "capabilities": self.capabilities(),
        ])
      case .reportFailure:
        self.emitCaptureDiagnostic(
          stage: "capture_failed",
          details: "no PCM after fallback taps=\(metrics.tap) failures=\(metrics.failures)"
        )
      }
    }
  }

  private func schedule(response: [String: Any]) throws {
    guard let responseId = response["responseId"] as? String,
          let generation = response["generation"] as? Int,
          let epoch = response["captureEpoch"] as? Int,
          epoch == captureEpoch,
          let wavBase64 = response["wavBase64"] as? String,
          let wav = Data(base64Encoded: wavBase64) else {
      throw Exception(name: "decode_failed", description: "Response binding or WAV payload is invalid")
    }
    guard engine.isRunning else {
      throw Exception(name: "playback_unavailable", description: "Audio engine is not running")
    }
    cancelCurrent(errorCode: nil)

    let file = FileManager.default.temporaryDirectory
      .appendingPathComponent("chronicle-response-\(UUID().uuidString).wav")
    try wav.write(to: file, options: .atomic)
    let audioFile = try AVAudioFile(forReading: file)
    currentResponse = (responseId, generation, file)
    captureSuppressed = capabilities()["mode"] as? String == "duplex_half"
    player.scheduleFile(audioFile, at: nil, completionCallbackType: .dataPlayedBack) { [weak self] _ in
      self?.controlQueue.async {
        guard let self,
              let current = self.currentResponse,
              current.id == responseId,
              current.generation == generation else { return }
        self.currentResponse = nil
        self.captureSuppressed = false
        try? FileManager.default.removeItem(at: file)
        self.emitPlayback(responseId, generation, state: "done", errorCode: nil)
      }
    }
    player.play()
    emitPlayback(responseId, generation, state: "started", errorCode: nil)
  }

  private func cancelCurrent(errorCode: String?) {
    guard let current = currentResponse else { return }
    player.stop()
    currentResponse = nil
    captureSuppressed = false
    try? FileManager.default.removeItem(at: current.file)
    emitPlayback(current.id, current.generation, state: "cancelled", errorCode: errorCode)
  }

  private func emitPlayback(
    _ responseId: String,
    _ generation: Int,
    state: String,
    errorCode: String?
  ) {
    sendEvent("onPlaybackState", [
      "responseId": responseId,
      "generation": generation,
      "captureEpoch": captureEpoch,
      "state": state,
      "monotonicTimestampMs": ProcessInfo.processInfo.systemUptime * 1_000,
      "errorCode": errorCode as Any,
    ])
  }

  private func capabilities() -> [String: Any] {
    let route = AVAudioSession.sharedInstance().currentRoute
    let input = route.inputs.first?.portType
    let output = route.outputs.first?.portType
    let isolated = output == .headphones || output == .bluetoothHFP || output == .usbAudio
    let speaker = output == .builtInSpeaker
    let full = speaker && voiceProcessingEnabled
    let mode = isolated ? "duplex_isolated" : (full ? "duplex_full" : "duplex_half")
    let aecAvailable = voiceProcessingEnabled && speaker
    return [
      "mode": mode,
      "input_route": inputRoute(input),
      "output_route": outputRoute(output),
      "native_sample_rate": Int(AVAudioSession.sharedInstance().sampleRate),
      "aec": effect(requested: speaker, available: aecAvailable, enabled: full),
      "noise_suppression": effect(
        requested: !isolated,
        available: voiceProcessingEnabled,
        enabled: voiceProcessingEnabled && !isolated
      ),
      "fallback_reason": (!isolated && !full) ? "aec_unavailable" : NSNull(),
    ]
  }

  private func effect(requested: Bool, available: Bool, enabled: Bool) -> [String: Bool] {
    ["requested": requested, "available": available, "enabled": enabled]
  }

  private func inputRoute(_ port: AVAudioSession.Port?) -> String {
    switch port {
    case .builtInMic: return "built_in_mic"
    case .bluetoothHFP: return "bluetooth_hfp"
    case .headsetMic: return "wired_mic"
    case .usbAudio: return "usb"
    default: return "unknown"
    }
  }

  private func outputRoute(_ port: AVAudioSession.Port?) -> String {
    switch port {
    case .builtInSpeaker: return "speakerphone"
    case .builtInReceiver: return "earpiece"
    case .headphones: return "headphones"
    case .bluetoothHFP: return "bluetooth_hfp"
    case .usbAudio: return "usb"
    default: return "unknown"
    }
  }

  @discardableResult
  private func tearDownEngine(deactivateSession: Bool) -> Bool {
    var restored = true
    captureWatchdogGeneration += 1
    cancelCurrent(errorCode: nil)
    setSessionRunning(false)
    if tapInstalled {
      engine.inputNode.removeTap(onBus: 0)
      tapInstalled = false
    }
    player.stop()
    engine.stop()
    if player.engine != nil { engine.detach(player) }
    pcmConverter = nil
    voiceProcessingEnabled = false
    captureSuppressed = false
    if deactivateSession {
      let session = AVAudioSession.sharedInstance()
      if let previousCategory, let previousMode {
        do {
          try session.setCategory(
            previousCategory,
            mode: previousMode,
            options: previousOptions
          )
        } catch {
          restored = false
        }
      }
      do {
        try session.setActive(false, options: .notifyOthersOnDeactivation)
      } catch {
        restored = false
      }
      sessionConfigured = false
      previousCategory = nil
      previousMode = nil
      previousOptions = []
    }
    return restored
  }

  private func installObservers() {
    let center = NotificationCenter.default
    observers.append(center.addObserver(
      forName: AVAudioSession.routeChangeNotification,
      object: nil,
      queue: nil
    ) { [weak self] _ in self?.systemChangePosted(reason: "route_changed", errorCode: "route_changed") })
    observers.append(center.addObserver(
      forName: AVAudioSession.interruptionNotification,
      object: nil,
      queue: nil
    ) { [weak self] _ in self?.systemChangePosted(reason: "interruption", errorCode: "playback_unavailable") })
    observers.append(center.addObserver(
      forName: NSNotification.Name.AVAudioEngineConfigurationChange,
      object: engine,
      queue: nil
    ) { [weak self] _ in self?.systemChangePosted(reason: "engine_reset", errorCode: "engine_reset") })
  }

  private func systemChangePosted(reason: String, errorCode: String) {
    let snapshot = captureMetrics()
    guard DuplexSystemNotificationPolicy.shouldHandle(
      sessionWasRunningWhenPosted: snapshot.running,
      pcmFrameCountWhenPosted: snapshot.pcm
    ) else { return }
    handleSystemChange(reason: reason, errorCode: errorCode)
  }

  private func handleSystemChange(reason: String, errorCode: String) {
    controlQueue.async { [weak self] in
      guard let self, self.isSessionRunning() else { return }
      let changedCapabilities = self.capabilities()
      self.cancelCurrent(errorCode: errorCode)
      self.tearDownEngine(deactivateSession: false)
      self.sendEvent("onRouteChange", [
        "captureEpoch": self.captureEpoch,
        "reason": reason,
        "capabilities": changedCapabilities,
      ])
    }
  }

  private func removeObservers() {
    observers.forEach(NotificationCenter.default.removeObserver)
    observers.removeAll()
  }
}

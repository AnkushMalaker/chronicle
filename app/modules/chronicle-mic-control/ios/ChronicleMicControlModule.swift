import AVFoundation
import ExpoModulesCore

public class ChronicleMicControlModule: Module {
  public func definition() -> ModuleDefinition {
    Name("ChronicleMicControl")

    // The system Mic Mode (Standard / Voice Isolation / Wide Spectrum) is a
    // per-app *user* choice made in Control Center; apps can only read it and
    // open the system picker. `preferred` is what the user picked, `active` is
    // what is actually in effect on the current audio route.
    AsyncFunction("getMicrophoneModeInfo") { () -> [String: String]? in
      guard #available(iOS 15.0, *) else { return nil }
      return [
        "preferred": Self.describe(AVCaptureDevice.preferredMicrophoneMode),
        "active": Self.describe(AVCaptureDevice.activeMicrophoneMode),
      ]
    }

    AsyncFunction("showMicrophoneModePicker") { () -> Bool in
      guard #available(iOS 15.0, *) else { return false }
      AVCaptureDevice.showSystemUserInterface(.microphoneModes)
      return true
    }.runOnQueue(.main)

    // Prefer an omnidirectional polar pattern on the built-in mic so far-field
    // room capture isn't narrowed toward the nearest talker. Must be called
    // while the audio session is active (i.e. after recording has started).
    // Deliberately a no-op when another input (e.g. a Bluetooth headset mic)
    // is active — polar patterns only apply to the built-in mic.
    AsyncFunction("applyFarFieldTuning") { () -> [String: Any] in
      let session = AVAudioSession.sharedInstance()
      guard let activeInput = session.currentRoute.inputs.first else {
        return ["applied": false, "reason": "no active input"]
      }
      guard activeInput.portType == .builtInMic else {
        return ["applied": false, "reason": "active input is \(activeInput.portType.rawValue)"]
      }
      let port = session.availableInputs?.first { $0.portType == .builtInMic } ?? activeInput
      guard let sources = port.dataSources, !sources.isEmpty else {
        return ["applied": false, "reason": "built-in mic has no selectable data sources"]
      }
      guard let omniSource = sources.first(where: {
        $0.supportedPolarPatterns?.contains(.omnidirectional) == true
      }) else {
        return ["applied": false, "reason": "no data source supports omnidirectional"]
      }
      do {
        try port.setPreferredDataSource(omniSource)
        try omniSource.setPreferredPolarPattern(.omnidirectional)
      } catch {
        return ["applied": false, "reason": error.localizedDescription]
      }
      return [
        "applied": true,
        "dataSource": omniSource.dataSourceName,
        "polarPattern": "omnidirectional",
      ]
    }
  }

  @available(iOS 15.0, *)
  private static func describe(_ mode: AVCaptureDevice.MicrophoneMode) -> String {
    switch mode {
    case .standard: return "standard"
    case .voiceIsolation: return "voiceIsolation"
    case .wideSpectrum: return "wideSpectrum"
    @unknown default: return "unknown"
    }
  }
}

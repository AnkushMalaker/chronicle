const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

function loadTypeScript(sourcePath, mocks) {
  const source = fs.readFileSync(sourcePath, 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      esModuleInterop: true,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      strict: true,
    },
    fileName: sourcePath,
  });
  const loaded = new Module(sourcePath, module);
  loaded.filename = sourcePath;
  loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath));
  const originalRequire = loaded.require.bind(loaded);
  loaded.require = (request) => mocks[request] ?? originalRequire(request);
  loaded._compile(compiled.outputText, sourcePath);
  return loaded.exports;
}

const writes = [];
const sourcePath = path.join(__dirname, '../src/services/phoneAudioDiagnostics.ts');
const { PhoneAudioDiagnostics } = loadTypeScript(sourcePath, {
  '@/utils/logger': {
    logInfo: (tag, message) => writes.push({ level: 'info', tag, message }),
    logWarn: (tag, message) => writes.push({ level: 'warn', tag, message }),
    logError: (tag, message) => writes.push({ level: 'error', tag, message }),
  },
});

const diagnostics = new PhoneAudioDiagnostics(() => 1_000);
diagnostics.beginAttempt();
diagnostics.listenerInstalled(1);
diagnostics.engineStarted(1, {
  mode: 'duplex_full',
  input_route: 'built_in_mic',
  output_route: 'speakerphone',
  native_sample_rate: 48_000,
});
diagnostics.nativeFrame({ captureEpoch: 1, opusBytes: 42, audioLevel: 0.25 });
diagnostics.nativeFrame({ captureEpoch: 1, opusBytes: 43, audioLevel: 0.5 });
diagnostics.audioLevelActive(0.5);
diagnostics.socketUnavailable(0);
diagnostics.socketUnavailable(0);
diagnostics.socketConnecting();
diagnostics.socketOpen();
diagnostics.captureStarted('capture-secret-id');
diagnostics.frameEnqueued(44);
diagnostics.packetAccepted(0);
diagnostics.timeout('meter_stalled');
diagnostics.failure('connect', 'wss://chronicle/ws/audio?token=secret-value');

assert.deepEqual(
  writes.map(({ level, tag }) => [level, tag]),
  [
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['warn', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['info', 'PhoneAudio'],
    ['warn', 'PhoneAudio'],
    ['error', 'PhoneAudio'],
  ],
  'each lifecycle boundary must be exported once while repeated frames/drops become counters',
);
const text = writes.map(({ message }) => message).join('\n');
assert.match(text, /button_pressed attempt=1/);
assert.match(text, /native_first_frame.*opus_bytes=42.*audio_level=0\.250/);
assert.match(text, /audio_level_active.*audio_level=0\.500/);
assert.match(text, /frame_dropped_socket_not_open.*ready_state=0/);
assert.match(text, /first_frame_enqueued.*opus_bytes=44/);
assert.match(text, /first_packet_accepted.*sequence=0/);
assert.match(
  text,
  /meter_stalled.*native_frames=2.*socket_drops=2.*enqueued_frames=1.*acked_packets=1.*last_audio_level=0\.500/,
);
assert.doesNotMatch(text, /capture-secret-id/, 'server-issued identifiers must be abbreviated');
assert.doesNotMatch(text, /secret-value/, 'credentials must be redacted from exported diagnostics');

const integrationSources = {
  recorder: fs.readFileSync(path.join(__dirname, '../src/hooks/usePhoneAudioRecorder.ts'), 'utf8'),
  streamer: fs.readFileSync(path.join(__dirname, '../src/hooks/useAudioStreamer.ts'), 'utf8'),
  orchestrator: fs.readFileSync(path.join(__dirname, '../src/hooks/useAudioStreamingOrchestrator.ts'), 'utf8'),
  ios: fs.readFileSync(path.join(__dirname, '../modules/chronicle-duplex-audio/ios/ChronicleDuplexAudioModule.swift'), 'utf8'),
  android: fs.readFileSync(path.join(__dirname, '../modules/chronicle-duplex-audio/android/src/main/java/com/chronicle/duplexaudio/ChronicleDuplexAudioModule.kt'), 'utf8'),
};
assert.match(integrationSources.recorder, /setAudioLevel\(/, 'native levels must drive the UI meter');
assert.match(integrationSources.recorder, /native_frame_timeout/, 'a silent native engine must surface a diagnostic');
assert.match(integrationSources.streamer, /packetAccepted\(sequence\)/, 'backend acknowledgements must be logged');
assert.match(integrationSources.orchestrator, /beginAttempt\(\)/, 'the phone button must open a diagnostic attempt');
assert.match(integrationSources.ios, /"audioLevel": audioLevel/, 'iOS must emit PCM audio levels');
assert.match(integrationSources.android, /"audioLevel" to DuplexAudioPolicy\.audioLevel/, 'Android must emit PCM audio levels');

console.log('phone audio diagnostics tests passed');

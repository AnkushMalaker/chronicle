const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

const appRoot = path.resolve(__dirname, '..');
const sourcePath = path.join(appRoot, 'src', 'protocol', 'voiceProtocol.ts');
const contractRoot = path.resolve(appRoot, '..', 'contracts', 'voice_protocol', 'v1');
const audioSourcePath = path.join(contractRoot, 'typescript', 'interactiveAudio.ts');
const nativeAdapterPath = path.join(appRoot, 'src', 'protocol', 'nativeAudioFrame.ts');

function loadTypeScriptModule(filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      strict: true,
    },
    fileName: filename,
    reportDiagnostics: true,
  });
  assert.equal(compiled.diagnostics?.length ?? 0, 0, 'protocol TypeScript must transpile');
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  loaded._compile(compiled.outputText, filename);
  return loaded.exports;
}

function fixtures(directory) {
  return fs.readdirSync(directory)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => [name, JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8'))]);
}

const { parseVoiceProtocolEvent } = loadTypeScriptModule(sourcePath);
const { InteractiveAudioFrameEncoder } = loadTypeScriptModule(audioSourcePath);
const { capturedAudioFrameFromNative } = loadTypeScriptModule(nativeAdapterPath);

for (const [name, fixture] of fixtures(path.join(contractRoot, 'golden'))) {
  assert.deepEqual(parseVoiceProtocolEvent(fixture), fixture, `${name} should be accepted`);
}

for (const [name, fixture] of fixtures(path.join(contractRoot, 'invalid'))) {
  assert.throws(() => parseVoiceProtocolEvent(fixture), undefined, `${name} should be rejected`);
}

const forgedIdentity = fixtures(path.join(contractRoot, 'golden'))[0][1];
assert.throws(
  () => parseVoiceProtocolEvent({ ...forgedIdentity, user_id: 'attacker-selected-user' }),
  /unknown protocol field: user_id/,
);

const encoder = new InteractiveAudioFrameEncoder(4);
const encoded = encoder.encode({
  captureEpoch: 4,
  codec: 'opus',
  payload: Uint8Array.from([0x48, 0x83, 0x7f]),
  sampleRate: 16000,
  channels: 1,
  frameDurationMs: 40,
  capturedAtMs: 1770000000125,
  monotonicTimestampMs: 4000,
});
assert.deepEqual(encoded.header, {
  type: 'audio-chunk',
  data: {
    rate: 16000,
    channels: 1,
    codec: 'opus',
    frame_duration_ms: 40,
    time_basis: 'captured',
    frame_sequence: 0,
    monotonic_offset_ms: 0,
    captured_at_ms: 1770000000125,
  },
  payload_length: 3,
});
assert.deepEqual(encoded.payload, Uint8Array.from([0x48, 0x83, 0x7f]));

const next = encoder.encode({
  captureEpoch: 4,
  codec: 'opus',
  payload: Uint8Array.from([0x48, 0x84]),
  sampleRate: 16000,
  channels: 1,
  frameDurationMs: 40,
  capturedAtMs: 1770000000165,
  monotonicTimestampMs: 4040,
});
assert.equal(next.header.data.frame_sequence, 1);
assert.equal(next.header.data.monotonic_offset_ms, 40);

const nativeFrame = capturedAudioFrameFromNative({
  captureEpoch: 7,
  capturedAtMs: 1770000002000,
  monotonicTimestampMs: 6000,
  sampleRate: 16000,
  channels: 1,
  codec: 'opus',
  frameDurationMs: 20,
  audioLevel: 0.25,
  payloadBase64: 'AAECAw==',
}, value => Uint8Array.from(Buffer.from(value, 'base64')));
assert.deepEqual(nativeFrame, {
  captureEpoch: 7,
  capturedAtMs: 1770000002000,
  monotonicTimestampMs: 6000,
  sampleRate: 16000,
  channels: 1,
  codec: 'opus',
  frameDurationMs: 20,
  audioLevel: 0.25,
  payload: Uint8Array.from([0, 1, 2, 3]),
});

console.log('voice protocol contract tests passed');

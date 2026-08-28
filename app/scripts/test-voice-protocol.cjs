const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

const appRoot = path.resolve(__dirname, '..');
const contractRoot = path.resolve(appRoot, '..', 'contracts', 'voice_protocol', 'v1');
const sourcePath = path.join(contractRoot, 'typescript', 'voiceProtocol.ts');
const pcmSourcePath = path.join(contractRoot, 'typescript', 'interactivePcm.ts');
const nativeAdapterPath = path.join(appRoot, 'src', 'protocol', 'nativePcmFrame.ts');

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
const {
  InteractivePcmFrameEncoder,
  selectInteractivePcmBufferSize,
} = loadTypeScriptModule(pcmSourcePath);
const { capturedPcmFrameFromNative } = loadTypeScriptModule(nativeAdapterPath);

for (const [name, fixture] of fixtures(path.join(contractRoot, 'golden'))) {
  if (fixture.type === 'audio-chunk') continue;
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

const goldenFrame = JSON.parse(
  fs.readFileSync(path.join(contractRoot, 'golden', 'interactive-pcm-frame.json'), 'utf8'),
);
const encoder = new InteractivePcmFrameEncoder(4);
const encoded = encoder.encode({
  captureEpoch: 4,
  pcm: new Uint8Array(1280),
  sampleRate: 16000,
  channels: 1,
  sampleWidth: 2,
  capturedAtMs: 1770000000125,
  monotonicTimestampMs: 4000,
});
assert.deepEqual(encoded.header, goldenFrame);
assert.equal(encoded.payload.byteLength, 1280);
assert.equal(selectInteractivePcmBufferSize(16000), 512);
assert.equal(selectInteractivePcmBufferSize(48000), 2048);
assert.throws(
  () => encoder.encode({
    captureEpoch: 4,
    pcm: new Uint8Array(8192),
    sampleRate: 16000,
    channels: 1,
    sampleWidth: 2,
    capturedAtMs: 1770000000165,
    monotonicTimestampMs: 4040,
  }),
  /20-100 ms/,
);

encoder.reset(5);
const reset = encoder.encode({
  captureEpoch: 5,
  pcm: new Uint8Array(640),
  sampleRate: 16000,
  channels: 1,
  sampleWidth: 2,
  capturedAtMs: 1770000001000,
  monotonicTimestampMs: 5000,
});
assert.equal(reset.header.data.frame_sequence, 0);
assert.equal(reset.header.data.monotonic_offset_ms, 0);

const nativeFrame = capturedPcmFrameFromNative({
  captureEpoch: 7,
  capturedAtMs: 1770000002000,
  monotonicTimestampMs: 6000,
  sampleRate: 16000,
  channels: 1,
  sampleWidth: 2,
  pcmBase64: 'AAECAw==',
}, value => Uint8Array.from(Buffer.from(value, 'base64')));
assert.deepEqual(nativeFrame, {
  captureEpoch: 7,
  capturedAtMs: 1770000002000,
  monotonicTimestampMs: 6000,
  sampleRate: 16000,
  channels: 1,
  sampleWidth: 2,
  pcm: Uint8Array.from([0, 1, 2, 3]),
});

console.log('voice protocol contract tests passed');

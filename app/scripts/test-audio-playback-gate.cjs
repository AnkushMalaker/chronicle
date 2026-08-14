const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

const sourcePath = path.join(__dirname, '../src/utils/audioPlaybackGate.ts');
const source = fs.readFileSync(sourcePath, 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
    strict: true,
  },
  fileName: sourcePath,
});
const gateModule = new Module(sourcePath, module);
gateModule.filename = sourcePath;
gateModule.paths = Module._nodeModulePaths(path.dirname(sourcePath));
gateModule._compile(compiled.outputText, sourcePath);

const {
  createDownlinkPlaybackCaptureGate,
  shouldForwardCapturedAudio,
} = gateModule.exports;

let now = 1_000;
const gate = createDownlinkPlaybackCaptureGate({
  now: () => now,
  tailMs: 350,
});

assert.equal(gate.shouldSuppressCapture(), false, 'capture starts unsuppressed');
assert.equal(shouldForwardCapturedAudio(3_200, gate), true, 'live mic audio is forwarded');
assert.equal(shouldForwardCapturedAudio(0, gate), false, 'empty audio is ignored');

const finishFirst = gate.beginPlayback();
assert.equal(gate.shouldSuppressCapture(), true, 'capture is suppressed during playback');
assert.equal(
  shouldForwardCapturedAudio(3_200, gate),
  false,
  'speaker playback frames never reach the uplink'
);

const finishSecond = gate.beginPlayback();
finishFirst();
assert.equal(
  gate.shouldSuppressCapture(),
  true,
  'one completed reply cannot unsuppress an overlapping reply'
);

finishSecond();
assert.equal(gate.shouldSuppressCapture(), true, 'capture stays suppressed for the echo tail');

now += 349;
assert.equal(gate.shouldSuppressCapture(), true, 'capture remains suppressed inside the tail');

now += 1;
assert.equal(gate.shouldSuppressCapture(), false, 'capture resumes when the tail expires');

finishSecond();
assert.equal(gate.shouldSuppressCapture(), false, 'completion is idempotent');

const finishThird = gate.beginPlayback();
now += 10_000;
assert.equal(gate.shouldSuppressCapture(), true, 'active playback never expires by wall clock');
finishThird();

console.log('audio playback capture gate: ok');

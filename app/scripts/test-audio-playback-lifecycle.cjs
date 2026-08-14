const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

function makePcm16WavBase64(durationMs, sampleRate = 16_000) {
  const channels = 1;
  const bitsPerSample = 16;
  const blockAlign = channels * (bitsPerSample / 8);
  const byteRate = sampleRate * blockAlign;
  const dataSize = Math.round((durationMs / 1_000) * byteRate);
  const wav = Buffer.alloc(44 + dataSize);

  wav.write('RIFF', 0, 'ascii');
  wav.writeUInt32LE(36 + dataSize, 4);
  wav.write('WAVE', 8, 'ascii');
  wav.write('fmt ', 12, 'ascii');
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(channels, 22);
  wav.writeUInt32LE(sampleRate, 24);
  wav.writeUInt32LE(byteRate, 28);
  wav.writeUInt16LE(blockAlign, 32);
  wav.writeUInt16LE(bitsPerSample, 34);
  wav.write('data', 36, 'ascii');
  wav.writeUInt32LE(dataSize, 40);
  return wav.toString('base64');
}

const sourcePath = path.join(__dirname, '../src/utils/audioPlayback.ts');
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

let listener;
let gateMaximumMs;
let gateReleaseCount = 0;
let playerRemoveCount = 0;
let subscriptionRemoveCount = 0;
let tempFileDeleteCount = 0;
const timers = new Map();
const logLines = [];
let nextTimerId = 1;

class MockFile {
  constructor(_directory, name) {
    this.exists = false;
    this.uri = `file:///cache/${name}`;
    this.bytes = new Uint8Array();
  }

  write(content, options) {
    assert.equal(options.encoding, 'base64');
    this.bytes = new Uint8Array(Buffer.from(content, 'base64'));
    this.exists = true;
  }

  bytesSync() {
    return this.bytes;
  }

  delete() {
    tempFileDeleteCount += 1;
    this.exists = false;
  }
}

const mocks = {
  'expo-audio': {
    createAudioPlayer: () => ({
      addListener: (eventName, callback) => {
        assert.equal(eventName, 'playbackStatusUpdate');
        listener = callback;
        return { remove: () => { subscriptionRemoveCount += 1; } };
      },
      play: () => {},
      remove: () => { playerRemoveCount += 1; },
    }),
    setAudioModeAsync: async () => {},
  },
  'expo-file-system': {
    File: MockFile,
    Paths: { cache: {} },
  },
  './audioPlaybackGate': {
    downlinkPlaybackCaptureGate: {
      beginPlayback: (maximumMs) => {
        gateMaximumMs = maximumMs;
        return () => { gateReleaseCount += 1; };
      },
    },
  },
  './logger': {
    logInfo: (tag, message) => { logLines.push(`INFO ${tag} ${message}`); },
    logWarn: (tag, message) => { logLines.push(`WARN ${tag} ${message}`); },
  },
};

const originalLoad = Module._load;
const originalSetTimeout = global.setTimeout;
const originalClearTimeout = global.clearTimeout;

Module._load = function mockLoad(request, parent, isMain) {
  if (Object.prototype.hasOwnProperty.call(mocks, request)) return mocks[request];
  return originalLoad.call(this, request, parent, isMain);
};
global.setTimeout = (callback, delay) => {
  const id = nextTimerId++;
  timers.set(id, { callback, delay });
  return id;
};
global.clearTimeout = (id) => {
  timers.delete(id);
};

const playbackModule = new Module(sourcePath, module);
playbackModule.filename = sourcePath;
playbackModule.paths = Module._nodeModulePaths(path.dirname(sourcePath));
playbackModule._compile(compiled.outputText, sourcePath);

(async () => {
  try {
    await playbackModule.exports.playDownlinkAudio({
      audio_b64: makePcm16WavBase64(1_000),
      format: 'wav',
    });

    assert.equal(typeof listener, 'function', 'the real playback completion listener is attached');
    assert.equal(
      gateMaximumMs,
      1_250,
      'a one-second local WAV bounds capture suppression to its duration plus startup grace'
    );
    assert.equal(timers.size, 1, 'one bounded cleanup timer is scheduled');

    const [{ callback, delay }] = timers.values();
    assert.equal(delay, 1_250, 'cleanup uses the same duration-derived deadline');

    callback();
    assert.equal(gateReleaseCount, 1, 'missing iOS completion callback cannot strand the gate');
    assert.equal(playerRemoveCount, 1, 'the unmanaged player is removed at the deadline');
    assert.equal(subscriptionRemoveCount, 1, 'the status listener is removed at the deadline');
    assert.equal(tempFileDeleteCount, 1, 'the staged WAV is deleted at the deadline');
    assert.ok(
      logLines.some((line) => line.includes('duration_timeout')),
      'the fallback release is preserved in device diagnostics'
    );

    listener({ didJustFinish: true });
    assert.equal(gateReleaseCount, 1, 'late completion remains idempotent');

    console.log('audio playback lifecycle: ok');
  } finally {
    Module._load = originalLoad;
    global.setTimeout = originalSetTimeout;
    global.clearTimeout = originalClearTimeout;
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

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

const values = new Map();
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const storage = {
  async getItem(key) {
    return values.get(key) ?? null;
  },
  async setItem(key, value) {
    await wait(value === '10' ? 5 : 20);
    values.set(key, value);
  },
  async removeItem(key) {
    values.delete(key);
  },
};

class MockDirectory {
  constructor() {
    this.exists = true;
  }

  list() {
    return [];
  }
}

class MockFile {
  constructor(_directory, name) {
    this.name = name;
    this.exists = false;
  }
}

const sourcePath = path.join(__dirname, '../src/services/durableAudioSpool.ts');
const { DurableAudioSpool } = loadTypeScript(sourcePath, {
  '@react-native-async-storage/async-storage': storage,
  'expo-file-system': {
    Directory: MockDirectory,
    File: MockFile,
    Paths: { document: '/documents' },
  },
});

(async () => {
  const spool = new DurableAudioSpool();
  spool.active = { file: { name: 'segment.spool' } };
  const packet = (sequence) => ({
    fileName: 'segment.spool',
    segmentId: 'segment',
    sequence,
    capturedAtMs: 1_770_000_000_000 + sequence,
    payload: new Uint8Array([sequence]),
  });

  await Promise.all([
    spool.acknowledge(packet(10)),
    spool.acknowledge(packet(3)),
  ]);

  assert.equal(
    values.get('chronicle.audioSpool.ack.segment.spool'),
    '10',
    'out-of-order concurrent ACKs must never lower the durable watermark',
  );
  console.log('durable audio spool tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

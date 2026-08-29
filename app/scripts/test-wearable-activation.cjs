const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

function loadTypeScript(sourcePath) {
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
  loaded._compile(compiled.outputText, sourcePath);
  return loaded.exports;
}

const activationPath = path.join(__dirname, '../src/services/wearableActivation.ts');
const {
  NEO_ACTIVE_CONTROL_UUID,
  NEO_ACTIVE_VALUE_BASE64,
  WEARABLE_SERVICE_UUID,
  activateWearableAfterConnect,
} = loadTypeScript(activationPath);

(async () => {
  const writes = [];
  const neoTransport = {
    characteristicsForDevice: async (deviceId, serviceUuid) => {
      assert.equal(deviceId, 'neo-1');
      assert.equal(serviceUuid, WEARABLE_SERVICE_UUID);
      return [{
        uuid: NEO_ACTIVE_CONTROL_UUID.toUpperCase(),
        isWritableWithResponse: true,
      }];
    },
    writeCharacteristicWithResponseForDevice: async (...args) => {
      writes.push(args);
    },
  };

  assert.equal(
    await activateWearableAfterConnect(neoTransport, 'neo-1'),
    'neo_activated',
  );
  assert.deepEqual(writes, [[
    'neo-1',
    WEARABLE_SERVICE_UUID,
    NEO_ACTIVE_CONTROL_UUID,
    NEO_ACTIVE_VALUE_BASE64,
  ]]);
  assert.equal(NEO_ACTIVE_VALUE_BASE64, 'AQ==', 'Neo Active is the single byte 0x01');

  let genericWriteCount = 0;
  const genericTransport = {
    characteristicsForDevice: async () => [{
      uuid: '19b10001-e8f2-537e-4f6c-d104768a1214',
      isWritableWithResponse: true,
    }],
    writeCharacteristicWithResponseForDevice: async () => {
      genericWriteCount += 1;
    },
  };
  assert.equal(
    await activateWearableAfterConnect(genericTransport, 'omi-1'),
    'not_required',
  );
  assert.equal(genericWriteCount, 0, 'ordinary OMI devices remain unchanged');

  const invalidNeoTransport = {
    characteristicsForDevice: async () => [{
      uuid: NEO_ACTIVE_CONTROL_UUID,
      isWritableWithResponse: false,
    }],
    writeCharacteristicWithResponseForDevice: async () => {
      throw new Error('must not attempt an unsupported write');
    },
  };
  await assert.rejects(
    activateWearableAfterConnect(invalidNeoTransport, 'neo-bad'),
    /does not support writes with response/,
  );

  let releaseWrite;
  const delayedWrite = new Promise((resolve) => {
    releaseWrite = resolve;
  });
  const delayedTransport = {
    characteristicsForDevice: async () => [{
      uuid: NEO_ACTIVE_CONTROL_UUID,
      isWritableWithResponse: true,
    }],
    writeCharacteristicWithResponseForDevice: async () => delayedWrite,
  };
  let activationFinished = false;
  const activation = activateWearableAfterConnect(delayedTransport, 'neo-slow')
    .then(() => { activationFinished = true; });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(activationFinished, false, 'connection readiness must wait for the Active write');
  releaseWrite();
  await activation;
  assert.equal(activationFinished, true);

  console.log('wearable activation contract tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

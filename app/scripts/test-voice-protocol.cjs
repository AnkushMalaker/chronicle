const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

const appRoot = path.resolve(__dirname, '..');
const sourcePath = path.join(appRoot, 'src', 'protocol', 'voiceProtocol.ts');
const contractRoot = path.resolve(appRoot, '..', 'contracts', 'voice_protocol', 'v1');

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

console.log('voice protocol contract tests passed');

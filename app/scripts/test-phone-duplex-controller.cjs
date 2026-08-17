const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

function loadTypeScript(sourcePath, mocks = {}) {
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

const protocolPath = path.join(__dirname, '../src/protocol/voiceProtocol.ts');
const protocol = loadTypeScript(protocolPath);
const controllerPath = path.join(__dirname, '../src/protocol/phoneDuplexController.ts');
const { PhoneDuplexController } = loadTypeScript(controllerPath, {
  './voiceProtocol': protocol,
});

const capabilities = {
  mode: 'duplex_full',
  input_route: 'built_in_mic',
  output_route: 'speakerphone',
  native_sample_rate: 48_000,
  aec: { requested: true, available: true, enabled: true },
  noise_suppression: { requested: true, available: true, enabled: true },
  fallback_reason: null,
};

let eventSequence = 0;
const uuid = (value) => `00000000-0000-4000-8000-${String(value).padStart(12, '0')}`;
const base = (type, eventId = uuid(++eventSequence)) => ({
  type,
  protocol: 1,
  event_id: eventId,
  client_id: 'client-1',
  sent_at: '2026-08-16T00:00:00.000Z',
});
const bound = (type, eventId) => ({
  ...base(type, eventId),
  audio_session_id: 'audio-1',
  voice_session_id: 'voice-1',
  capture_epoch: 4,
});

(async () => {
  const appConfig = JSON.parse(
    fs.readFileSync(path.join(__dirname, '../app.json'), 'utf8'),
  );
  assert.deepEqual(
    appConfig.expo.ios.infoPlist.UIBackgroundModes,
    ['audio'],
    'the phone engine must not advertise an unimplemented iOS background-processing mode',
  );

  const sent = [];
  const scheduled = [];
  const cancelled = [];
  const replacementSessions = [];
  const native = {
    scheduleResponse: async (response) => scheduled.push(response),
    cancelResponse: async (responseId, generation) => cancelled.push({ responseId, generation }),
    stopVoiceSession: async () => ({ restorationSucceeded: true, failureCode: null }),
  };
  let phoneEvent = 0;
  const controller = new PhoneDuplexController({
    capabilities,
    captureEpoch: 4,
    native,
    send: async (event) => sent.push(event),
    restartCapture: async () => ({
      captureEpoch: 5,
      capabilities: {
        ...capabilities,
        mode: 'duplex_isolated',
        output_route: 'headphones',
        aec: { requested: false, available: false, enabled: false },
        noise_suppression: { requested: false, available: true, enabled: false },
      },
    }),
    replaceAudioSession: async (binding, voiceSessionId) => {
      replacementSessions.push({ binding, voiceSessionId });
    },
    createEventId: () => uuid(1_000 + ++phoneEvent),
    now: () => new Date('2026-08-16T00:00:01.000Z'),
  });

  assert.equal(controller.protocolHandshakeComplete, false, 'old backend is not assumed compatible');
  await controller.receiveControl({
    ...base('audio-session.started'),
    audio_session_id: 'audio-1',
    capture_epoch: 4,
    processing_profile: 'duplex_aec',
    voice_session_id: null,
  });
  assert.equal(controller.protocolHandshakeComplete, true);

  const start = {
    ...bound('voice-session.start', uuid(100)),
    resume_token: 'a'.repeat(43),
    response_generation: 1,
    readiness_deadline_ms: 2_000,
  };
  await controller.receiveControl(start);
  await controller.receiveControl(start);
  assert.equal(sent.length, 1, 'duplicate server events are idempotent');
  assert.equal(sent[0].type, 'voice-session.ready');
  assert.deepEqual(sent[0].capabilities, capabilities);

  await controller.receiveControl({
    ...bound('response.audio'),
    turn_id: 'turn-1',
    turn_revision: 0,
    response_id: 'response-1',
    generation: 1,
    sequence: 0,
    kind: 'speech',
    barge_in_allowed: true,
    media_type: 'audio/wav',
    sample_rate: 16_000,
    byte_length: 4,
    duration_ms: 20,
    payload_length: 4,
    trace_id: 'trace-1',
    causation_id: 'cause-1',
  });
  await controller.receiveBinary(Uint8Array.from([1, 2, 3, 4]));
  assert.deepEqual(scheduled, [{
    responseId: 'response-1',
    generation: 1,
    captureEpoch: 4,
    wavBase64: 'AQIDBA==',
  }]);

  await controller.nativePlaybackChanged({
    responseId: 'response-1',
    generation: 1,
    captureEpoch: 4,
    state: 'started',
    monotonicTimestampMs: 10.4,
    errorCode: null,
  });
  assert.equal(sent.at(-1).type, 'response.playback');
  assert.equal(sent.at(-1).monotonic_timestamp_ms, 10);

  await controller.receiveControl({
    ...bound('response.cancel'),
    response_id: 'response-1',
    generation: 2,
    reason: 'barge_in',
  });
  assert.deepEqual(cancelled, [{ responseId: 'response-1', generation: 2 }]);

  await controller.nativeRouteChanged({
    captureEpoch: 4,
    reason: 'route_changed',
    capabilities: {
      ...capabilities,
      mode: 'duplex_isolated',
      output_route: 'headphones',
      aec: { requested: false, available: false, enabled: false },
      noise_suppression: { requested: false, available: true, enabled: false },
    },
  });
  assert.equal(replacementSessions.length, 1);
  assert.equal(replacementSessions[0].binding.captureEpoch, 5);
  assert.equal(replacementSessions[0].voiceSessionId, 'voice-1');
  assert.notEqual(sent.at(-1).type, 'voice-session.capabilities-changed');
  await controller.receiveControl({
    ...base('audio-session.started'),
    audio_session_id: 'audio-2',
    capture_epoch: 5,
    processing_profile: 'duplex_isolated',
    voice_session_id: 'voice-1',
  });
  assert.equal(sent.at(-1).type, 'voice-session.capabilities-changed');
  assert.equal(sent.at(-1).audio_session_id, 'audio-2');
  assert.equal(sent.at(-1).capture_epoch, 5);

  await assert.rejects(
    controller.receiveControl({
      ...bound('response.cancel'),
      audio_session_id: 'stale-audio',
      capture_epoch: 5,
      response_id: 'response-1',
      generation: 1,
      reason: 'disconnect',
    }),
    /active voice binding/,
  );

  await controller.close();
  const beforeStaleDelivery = scheduled.length;
  await controller.receiveBinary(Uint8Array.from([9]));
  assert.equal(scheduled.length, beforeStaleDelivery, 'closed socket cannot deliver stale media');

  const reconnected = new PhoneDuplexController({
    capabilities,
    captureEpoch: 6,
    native,
    resumeProof: controller.resumeProof,
    send: async (event) => sent.push(event),
  });
  await reconnected.receiveControl({
    ...base('audio-session.started'),
    audio_session_id: 'audio-3',
    capture_epoch: 6,
    processing_profile: 'duplex_aec',
    voice_session_id: 'voice-1',
  });
  assert.equal(sent.at(-1).type, 'voice-session.resume');
  assert.equal(sent.at(-1).previous_capture_epoch, 5);
  assert.equal(sent.at(-1).last_response_generation, 3);
  await reconnected.receiveControl({
    ...base('voice-session.start'),
    audio_session_id: 'audio-3',
    voice_session_id: 'voice-2',
    capture_epoch: 6,
    resume_token: 'b'.repeat(43),
    response_generation: 3,
    readiness_deadline_ms: 2_000,
  });
  assert.equal(sent.at(-1).type, 'voice-session.ready');
  await assert.rejects(
    reconnected.receiveBinary(Uint8Array.from([1, 2, 3, 4])),
    /without response.audio/,
    'a reconnect never inherits pending response media',
  );

  const expired = new PhoneDuplexController({
    capabilities,
    captureEpoch: 6,
    native,
    resumeProof: controller.resumeProof,
    send: async (event) => sent.push(event),
  });
  expired.prepareFreshCapture({ captureEpoch: 7, capabilities });
  await expired.receiveControl({
    ...base('audio-session.started'),
    audio_session_id: 'audio-4',
    capture_epoch: 7,
    processing_profile: 'duplex_aec',
    voice_session_id: null,
  });
  await expired.receiveControl({
    ...base('voice-session.start'),
    audio_session_id: 'audio-4',
    voice_session_id: 'voice-3',
    capture_epoch: 7,
    resume_token: 'c'.repeat(43),
    response_generation: 4,
    readiness_deadline_ms: 2_000,
  });
  assert.equal(sent.at(-1).type, 'voice-session.ready');

  console.log('phone duplex controller: ok');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

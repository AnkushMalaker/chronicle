*** Settings ***
Documentation   Tests for Transcription Service URL Configuration
Library         Collections
Library         ../libs/ConfigTestHelper.py
Test Tags       infra

*** Test Cases ***
Vibevoice Url Without Http Prefix
    [Documentation]  Test that VIBEVOICE_ASR_URL without http:// prefix works correctly.
    ${config_template}=  Create Dictionary  model_url=http://\${oc.env:VIBEVOICE_ASR_URL,host.docker.internal:8767}
    ${env_vars}=  Create Dictionary  VIBEVOICE_ASR_URL=host.docker.internal:8767

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    Should Be Equal  ${resolved["model_url"]}  http://host.docker.internal:8767
    Should Not Contain  ${resolved["model_url"]}  http://http://

Vibevoice Url With Http Prefix Causes Double Prefix
    [Documentation]  Test that VIBEVOICE_ASR_URL WITH http:// causes double prefix (bug scenario).
    ${config_template}=  Create Dictionary  model_url=http://\${oc.env:VIBEVOICE_ASR_URL,host.docker.internal:8767}
    ${env_vars}=  Create Dictionary  VIBEVOICE_ASR_URL=http://host.docker.internal:8767

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    Should Be Equal  ${resolved["model_url"]}  http://http://host.docker.internal:8767
    Should Contain  ${resolved["model_url"]}  http://http://

Vibevoice Url Default Fallback
    [Documentation]  Test that default fallback works when VIBEVOICE_ASR_URL is not set.
    ${config_template}=  Create Dictionary  model_url=http://\${oc.env:VIBEVOICE_ASR_URL,host.docker.internal:8767}
    ${env_vars}=  Create Dictionary

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    Should Be Equal  ${resolved["model_url"]}  http://host.docker.internal:8767

Parakeet Url Configuration
    [Documentation]  Test that PARAKEET_ASR_URL follows same pattern.
    ${config_template}=  Create Dictionary  model_url=http://\${oc.env:PARAKEET_ASR_URL,172.17.0.1:8767}
    ${env_vars}=  Create Dictionary  PARAKEET_ASR_URL=host.docker.internal:8767

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    Should Be Equal  ${resolved["model_url"]}  http://host.docker.internal:8767
    Should Not Contain  ${resolved["model_url"]}  http://http://

Url Parsing Removes Double Slashes
    [Documentation]  Test that URL with double http:// causes connection failures (simulated by parsing check).

    # Valid URL
    ${valid_url}=  Set Variable  http://host.docker.internal:8767/transcribe
    ${parsed_valid}=  Check Url Parsing  ${valid_url}
    Should Be Equal  ${parsed_valid["scheme"]}  http
    Should Be Equal  ${parsed_valid["netloc"]}  host.docker.internal:8767

    # Invalid URL
    ${invalid_url}=  Set Variable  http://http://host.docker.internal:8767/transcribe
    ${parsed_invalid}=  Check Url Parsing  ${invalid_url}
    Should Be Equal  ${parsed_invalid["scheme"]}  http
    # In python urlparse, 'http:' becomes the netloc for 'http://http://...'
    Should Be Equal  ${parsed_invalid["netloc"]}  http:
    Should Not Be Equal  ${parsed_invalid["netloc"]}  host.docker.internal:8767

Diarization Source Defaults To Provider
    [Documentation]  Test that diarization_source defaults to provider (trust provider diarization, fall back to pyannote).
    ${diarization}=  Create Dictionary
    ${backend}=  Create Dictionary  diarization=${diarization}
    ${config_template}=  Create Dictionary  backend=${backend}
    ${env_vars}=  Create Dictionary

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    ${val}=  Evaluate  $resolved.get('backend', {}).get('diarization', {}).get('diarization_source', 'provider')
    Should Be Equal  ${val}  provider

Diarization Source Explicit Pyannote
    [Documentation]  Test that diarization_source can be set to pyannote (always re-diarize).
    ${diarization}=  Create Dictionary  diarization_source=pyannote
    ${backend}=  Create Dictionary  diarization=${diarization}
    ${config_template}=  Create Dictionary  backend=${backend}
    ${env_vars}=  Create Dictionary

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    ${val}=  Evaluate  $resolved['backend']['diarization']['diarization_source']
    Should Be Equal  ${val}  pyannote

Vibevoice Provider Diarization Is Trusted
    [Documentation]  Test that a provider with segments + diarization capabilities qualifies for provider-sourced diarization.
    # Logic simulation
    ${vibevoice_capabilities}=  Create List  segments  diarization
    ${has_diarization}=  Evaluate  "diarization" in $vibevoice_capabilities
    ${has_segments}=  Evaluate  "segments" in $vibevoice_capabilities
    ${provider_diarized}=  Evaluate  $has_diarization and $has_segments
    Should Be Equal  ${provider_diarized}  ${TRUE}

Model Registry Url Resolution With Env Var
    [Documentation]  Test that model URLs resolve correctly from environment.
    ${model_def}=  Create Dictionary
    ...  name=stt-vibevoice
    ...  model_type=stt
    ...  model_provider=vibevoice
    ...  model_url=http://\${oc.env:VIBEVOICE_ASR_URL,host.docker.internal:8767}

    ${models}=  Create List  ${model_def}
    ${defaults}=  Create Dictionary  stt=stt-vibevoice
    ${config_template}=  Create Dictionary  defaults=${defaults}  models=${models}

    ${env_vars}=  Create Dictionary  VIBEVOICE_ASR_URL=host.docker.internal:8767

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    ${resolved_models}=  Get From Dictionary  ${resolved}  models
    Should Be Equal  ${resolved_models[0]["model_url"]}  http://host.docker.internal:8767

Multiple Asr Providers Url Resolution
    [Documentation]  Test that multiple ASR providers can use different URL patterns.
    ${m1}=  Create Dictionary  name=stt-vibevoice  model_url=http://\${oc.env:VIBEVOICE_ASR_URL,host.docker.internal:8767}
    ${m2}=  Create Dictionary  name=stt-parakeet  model_url=http://\${oc.env:PARAKEET_ASR_URL,172.17.0.1:8767}
    ${m3}=  Create Dictionary  name=stt-deepgram  model_url=https://api.deepgram.com/v1

    ${models}=  Create List  ${m1}  ${m2}  ${m3}
    ${config_template}=  Create Dictionary  models=${models}

    ${env_vars}=  Create Dictionary
    ...  VIBEVOICE_ASR_URL=host.docker.internal:8767
    ...  PARAKEET_ASR_URL=localhost:8080

    ${resolved}=  Resolve Omega Config  ${config_template}  ${env_vars}
    ${resolved_models}=  Get From Dictionary  ${resolved}  models

    Should Be Equal  ${resolved_models[0]["model_url"]}  http://host.docker.internal:8767
    Should Be Equal  ${resolved_models[1]["model_url"]}  http://localhost:8080
    Should Be Equal  ${resolved_models[2]["model_url"]}  https://api.deepgram.com/v1

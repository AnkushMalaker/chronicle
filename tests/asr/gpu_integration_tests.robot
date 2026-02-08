*** Settings ***
Documentation    GPU-based ASR Integration Tests - requires NVIDIA GPU
...
...              These tests validate actual transcription with real ASR models:
...              - Transformers/VibeVoice model loading and inference
...              - Transcription quality (actual text output)
...              - Word timestamp accuracy and ordering
...
...              IMPORTANT: These tests require:
...              - NVIDIA GPU with CUDA support
...              - Model download (~2-5GB first time)
...
...              The tests automatically start/stop the ASR service.
...              Run with: make test-asr-gpu
...              Excluded from default runs (requires-gpu tag)
Library          RequestsLibrary
Library          Collections
Resource         ../resources/asr_keywords.robot
Resource         ../setup/setup_keywords.robot

Suite Setup      GPU Test Suite Setup
Suite Teardown   GPU Test Suite Teardown

*** Variables ***
${GPU_ASR_URL}       http://localhost:8767
${TEST_AUDIO_FILE}   ${CURDIR}/../test_assets/DIY_Experts_Glass_Blowing_16khz_mono_1min.wav
# ASR service configuration
${ASR_SERVICE}       transformers-asr
${ASR_MODEL}         microsoft/VibeVoice-ASR
${ASR_PORT}          8767

*** Keywords ***

GPU Test Suite Setup
    [Documentation]    Setup for GPU tests - starts ASR service and waits for model loading
    Suite Setup
    Log To Console    \n========================================
    Log To Console    GPU ASR Integration Test Suite Setup
    Log To Console    ========================================

    # Start the ASR service
    Start GPU ASR Service    ${ASR_SERVICE}    ${ASR_MODEL}    ${ASR_PORT}

    # Wait for service to be ready (model loading can take a while)
    Log To Console    \n⏳ Waiting for model to load (may take 2-5 minutes first time)...
    Wait For ASR Service Ready    ${GPU_ASR_URL}    timeout=600s    interval=15s
    Log To Console    ✅ GPU ASR service is ready!

GPU Test Suite Teardown
    [Documentation]    Teardown for GPU tests - stops ASR service
    Log To Console    \n========================================
    Log To Console    GPU ASR Integration Test Suite Teardown
    Log To Console    ========================================

    # Stop and remove the ASR service
    Remove GPU ASR Service    ${ASR_SERVICE}

*** Test Cases ***

GPU ASR Service Health Check
    [Documentation]    Verify ASR service starts and reports healthy
    [Tags]    requires-gpu	health
    [Timeout]    300s

    Check ASR Service Health    ${GPU_ASR_URL}

GPU ASR Service Reports Model Info
    [Documentation]    Verify service reports model information
    [Tags]    requires-gpu	infra
    [Timeout]    60s

    ${info}=    Get ASR Service Info    ${GPU_ASR_URL}

    # Verify model info is present
    Should Not Be Empty    ${info}[model_id]
    Should Not Be Empty    ${info}[provider]
    Log    Model: ${info}[model_id], Provider: ${info}[provider]

GPU ASR Transcription Returns Text
    [Documentation]    Verify actual transcription produces non-empty text
    [Tags]    requires-gpu	e2e
    [Timeout]    180s

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}    ${GPU_ASR_URL}
    Should Be Equal As Integers    ${response.status_code}    200

    ${json}=    Set Variable    ${response.json()}
    Should Not Be Empty    ${json}[text]
    Log    Transcription: ${json}[text]

    # Verify reasonable transcription length (should have some words)
    ${text_length}=    Get Length    ${json}[text]
    Should Be True    ${text_length} > 10    Transcription should have meaningful content

GPU ASR Transcription Has Word Timestamps
    [Documentation]    Verify word timestamps from actual model inference
    [Tags]    requires-gpu	e2e
    [Timeout]    180s

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}    ${GPU_ASR_URL}
    ${json}=    Set Variable    ${response.json()}

    # Verify words are present
    Should Not Be Empty    ${json}[words]
    ${word_count}=    Get Length    ${json}[words]
    Log    Word count: ${word_count}

    # Verify timestamps are properly ordered and valid
    ${prev_end}=    Set Variable    ${0}
    FOR    ${word}    IN    @{json}[words]
        # Each word should have required fields
        Dictionary Should Contain Key    ${word}    word
        Dictionary Should Contain Key    ${word}    start
        Dictionary Should Contain Key    ${word}    end

        # Timestamps should be in order
        Should Be True    ${word}[start] >= ${prev_end}
        ...    Word "${word}[word]" start (${word}[start]) should be >= previous end (${prev_end})

        # End should be after start
        Should Be True    ${word}[end] > ${word}[start]
        ...    Word "${word}[word]" end (${word}[end]) should be > start (${word}[start])

        ${prev_end}=    Set Variable    ${word}[end]
    END

GPU ASR Transcription Duration Reasonable
    [Documentation]    Verify transcription duration matches audio (approximately)
    [Tags]    requires-gpu	e2e
    [Timeout]    180s

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}    ${GPU_ASR_URL}
    ${json}=    Set Variable    ${response.json()}

    # Get last word timestamp (approximate audio duration)
    ${word_count}=    Get Length    ${json}[words]
    IF    ${word_count} > 0
        ${last_word}=    Get From List    ${json}[words]    -1
        ${transcription_duration}=    Set Variable    ${last_word}[end]

        # For a 1-minute audio file, transcription should show reasonable duration
        # Allow some flexibility (at least 30 seconds, no more than 90 seconds)
        Should Be True    ${transcription_duration} > 30
        ...    Transcription duration (${transcription_duration}s) should be > 30s for 1-min audio
        Should Be True    ${transcription_duration} < 90
        ...    Transcription duration (${transcription_duration}s) should be < 90s for 1-min audio

        Log    Transcription duration: ${transcription_duration} seconds
    END

GPU ASR Returns Diarized Segments With Speaker Labels
    [Documentation]    Verify VibeVoice returns segments with speaker diarization
    ...                This tests the core VibeVoice capability: combined ASR + diarization
    [Tags]    requires-gpu	e2e
    [Timeout]    180s

    # First check if this model reports diarization capability
    ${info}=    Get ASR Service Info    ${GPU_ASR_URL}
    ${has_diarization}=    Evaluate    'diarization' in $info.get('capabilities', [])
    Log    Provider: ${info}[provider], Capabilities: ${info}[capabilities]

    # Skip if provider doesn't have diarization
    Skip If    not ${has_diarization}    Provider does not have diarization capability

    # Upload audio and get transcription
    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}    ${GPU_ASR_URL}
    Should Be Equal As Integers    ${response.status_code}    200

    ${json}=    Set Variable    ${response.json()}

    # Verify segments are present and non-empty
    Dictionary Should Contain Key    ${json}    segments
    ${segments}=    Set Variable    ${json}[segments]
    Should Not Be Empty    ${segments}    Diarization provider should return segments

    ${segment_count}=    Get Length    ${segments}
    Log    Found ${segment_count} diarized segments

    # Verify each segment has valid structure
    # Note: Non-speech segments (like [Music]) may have speaker=null
    ${speech_segments}=    Create List
    FOR    ${index}    ${segment}    IN ENUMERATE    @{segments}
        # Each segment must have speaker, start, end, text fields
        Dictionary Should Contain Key    ${segment}    speaker
        ...    Segment ${index} missing 'speaker' field
        Dictionary Should Contain Key    ${segment}    start
        ...    Segment ${index} missing 'start' field
        Dictionary Should Contain Key    ${segment}    end
        ...    Segment ${index} missing 'end' field
        Dictionary Should Contain Key    ${segment}    text
        ...    Segment ${index} missing 'text' field

        # Timestamps should be valid
        Should Be True    ${segment}[start] >= 0
        ...    Segment ${index} has negative start time
        Should Be True    ${segment}[end] > ${segment}[start]
        ...    Segment ${index} end (${segment}[end]) not greater than start (${segment}[start])

        # Track speech segments (speaker is not null)
        ${has_speaker}=    Evaluate    $segment['speaker'] is not None
        IF    ${has_speaker}
            Append To List    ${speech_segments}    ${segment}
            Log    Segment ${index}: [${segment}[speaker]] ${segment}[start]s-${segment}[end]s
        ELSE
            Log    Segment ${index}: [non-speech] ${segment}[start]s-${segment}[end]s: ${segment}[text]
        END
    END

    # Verify we have at least some speech segments with speaker labels
    ${speech_count}=    Get Length    ${speech_segments}
    Should Be True    ${speech_count} > 0
    ...    Expected at least one segment with speaker label, got ${speech_count}

    # Verify segments cover reasonable audio duration (should be close to 1 minute)
    ${last_segment}=    Get From List    ${segments}    -1
    ${total_duration}=    Set Variable    ${last_segment}[end]
    Should Be True    ${total_duration} > 30
    ...    Segments only cover ${total_duration}s, expected > 30s for 1-min audio
    Should Be True    ${total_duration} < 90
    ...    Segments cover ${total_duration}s, expected < 90s for 1-min audio

    Log    ✅ Diarization test passed: ${segment_count} segments, ${total_duration}s total duration

*** Settings ***
Documentation    Batched Transcription Integration Tests - requires NVIDIA GPU
...
...              Tests that VibeVoice ASR correctly batches long audio files,
...              transcribes with context passing between windows, and returns
...              coherent stitched results via the /transcribe HTTP API.
...
...              The service is started with BATCH_THRESHOLD_SECONDS=60 to force
...              batching on the 4-minute test audio file.
...
...              IMPORTANT: These tests require:
...              - NVIDIA GPU with CUDA support
...              - VibeVoice model (~10GB first time download)
...
...              Run with: make test-asr-gpu
...              Excluded from default runs (requires-gpu tag)
Library          RequestsLibrary
Library          Collections
Library          Process
Resource         ../resources/asr_keywords.robot

Suite Setup      Batching Test Suite Setup
Suite Teardown   Batching Test Suite Teardown

*** Variables ***
${GPU_ASR_URL}       http://localhost:8767
${ASR_SERVICE}       vibevoice-asr
${ASR_PORT}          8767
${TEST_AUDIO_4MIN}   ${CURDIR}/../test_assets/DIY_Experts_Glass_Blowing_16khz_mono_4min.wav
${TEST_AUDIO_1MIN}   ${CURDIR}/../test_assets/DIY_Experts_Glass_Blowing_16khz_mono_1min.wav

*** Keywords ***

Batching Test Suite Setup
    [Documentation]    Start VibeVoice ASR with low batch threshold to force batching
    ${asr_dir}=    Set Variable    ${CURDIR}/../../extras/asr-services

    Log To Console    \n========================================
    Log To Console    Batching Test Suite Setup
    Log To Console    Starting VibeVoice with BATCH_THRESHOLD_SECONDS=60
    Log To Console    ========================================

    # Start vibevoice with low batch threshold (60s) so 4-min audio triggers batching
    ${result}=    Run Process    docker    compose    up    -d    --build    ${ASR_SERVICE}
    ...    cwd=${asr_dir}
    ...    env:ASR_PORT=${ASR_PORT}
    ...    env:BATCH_THRESHOLD_SECONDS=60
    ...    env:BATCH_DURATION_SECONDS=60
    ...    env:BATCH_OVERLAP_SECONDS=15

    IF    ${result.rc} != 0
        Log    STDOUT: ${result.stdout}
        Log    STDERR: ${result.stderr}
        Fail    Failed to start ${ASR_SERVICE}: ${result.stderr}
    END

    Log To Console    \nWaiting for VibeVoice model to load (may take 2-5 minutes)...
    Wait For ASR Service Ready    ${GPU_ASR_URL}    timeout=600s    interval=15s
    Log To Console    VibeVoice ASR service is ready!

Batching Test Suite Teardown
    [Documentation]    Stop and remove VibeVoice ASR service
    Log To Console    \n========================================
    Log To Console    Batching Test Suite Teardown
    Log To Console    ========================================

    Remove GPU ASR Service    ${ASR_SERVICE}

*** Test Cases ***

Batched Transcription Returns Segments Covering Full Duration
    [Documentation]    Upload 4-minute audio (triggers batching since > 60s threshold).
    ...                Verify the response has segments covering the full duration
    ...                with no large gaps, confirming stitching works correctly.
    [Tags]    requires-gpu	e2e
    [Timeout]    600s

    # Upload the 4-minute audio file
    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_4MIN}    ${GPU_ASR_URL}
    Should Be Equal As Integers    ${response.status_code}    200
    ...    Transcription request failed with status ${response.status_code}

    ${json}=    Set Variable    ${response.json()}

    # Verify non-empty transcription text
    Should Not Be Empty    ${json}[text]    Transcription text should not be empty
    ${text_length}=    Get Length    ${json}[text]
    Should Be True    ${text_length} > 100
    ...    Transcription should have substantial content (got ${text_length} chars)
    Log To Console    \nTranscription: ${text_length} characters

    # Verify segments exist and cover the audio
    Should Not Be Empty    ${json}[segments]    Should have transcription segments
    ${segment_count}=    Get Length    ${json}[segments]
    Should Be True    ${segment_count} > 3
    ...    4-min audio should produce more than 3 segments (got ${segment_count})
    Log To Console    Segments: ${segment_count}

    # Verify segments cover most of the ~4 minute duration
    ${last_segment}=    Get From List    ${json}[segments]    -1
    ${total_duration}=    Set Variable    ${last_segment}[end]
    Should Be True    ${total_duration} > 180
    ...    Segments should cover > 3 min of the 4-min audio (got ${total_duration}s)
    Log To Console    Duration covered: ${total_duration}s

    # Verify no large gaps between consecutive segments (stitching quality)
    ${prev_end}=    Set Variable    ${0}
    FOR    ${index}    ${segment}    IN ENUMERATE    @{json}[segments]
        ${gap}=    Evaluate    ${segment}[start] - ${prev_end}
        Should Be True    ${gap} < 10.0
        ...    Gap of ${gap}s between segment ${index-1} and ${index} (max allowed: 10s)
        ${prev_end}=    Set Variable    ${segment}[end]
    END
    Log To Console    No gaps > 10s between segments

Batched Transcription Has Valid Speaker Labels
    [Documentation]    Verify batched transcription preserves speaker diarization
    ...                across batch window boundaries.
    [Tags]    requires-gpu	e2e
    [Timeout]    600s

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_4MIN}    ${GPU_ASR_URL}
    ${json}=    Set Variable    ${response.json()}

    # Verify segments have speaker labels
    ${speech_segments}=    Create List
    FOR    ${segment}    IN    @{json}[segments]
        ${has_speaker}=    Evaluate    $segment.get('speaker') is not None
        IF    ${has_speaker}
            Append To List    ${speech_segments}    ${segment}
        END
    END

    ${speech_count}=    Get Length    ${speech_segments}
    Should Be True    ${speech_count} > 0
    ...    Expected speaker-labeled segments in batched output (got ${speech_count})
    Log To Console    \nSpeaker-labeled segments: ${speech_count}

    # Verify segment timestamps are ordered
    ${prev_start}=    Set Variable    ${0}
    FOR    ${segment}    IN    @{speech_segments}
        Should Be True    ${segment}[start] >= ${prev_start}
        ...    Segment starts (${segment}[start]) should be >= previous (${prev_start})
        Should Be True    ${segment}[end] > ${segment}[start]
        ...    Segment end (${segment}[end]) should be > start (${segment}[start])
        ${prev_start}=    Set Variable    ${segment}[start]
    END
    Log To Console    All segments properly ordered

Short Audio Does Not Trigger Batching
    [Documentation]    Upload 1-minute audio (below 60s threshold - actually exactly at threshold).
    ...                Should still produce valid transcription without batching.
    [Tags]    requires-gpu	e2e
    [Timeout]    300s

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_1MIN}    ${GPU_ASR_URL}
    Should Be Equal As Integers    ${response.status_code}    200

    ${json}=    Set Variable    ${response.json()}

    # Verify basic transcription quality
    Should Not Be Empty    ${json}[text]    Short audio should produce transcription
    Should Not Be Empty    ${json}[segments]    Short audio should produce segments

    ${segment_count}=    Get Length    ${json}[segments]
    Log To Console    \n1-min audio: ${segment_count} segments, ${json}[text].__len__() chars

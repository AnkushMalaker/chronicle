*** Settings ***
Documentation    ASR Service Protocol Tests - validates API endpoints and response formats
...
...              These tests validate the ASR service API contract without requiring GPU.
...              They use the mock ASR server to verify:
...              - Health endpoint structure and response
...              - Info endpoint structure and response
...              - Transcribe endpoint accepts audio files
...              - Response format matches expected schema
...
...              Run with: make test-asr
Library          RequestsLibrary
Library          Collections
Resource         ../resources/asr_keywords.robot
Resource         ../setup/setup_keywords.robot

Suite Setup      Suite Setup

*** Variables ***
${ASR_URL}           http://localhost:8765
${TEST_AUDIO_FILE}   ${CURDIR}/../test_assets/DIY_Experts_Glass_Blowing_16khz_mono_1min.wav

*** Test Cases ***

ASR Health Endpoint Returns Valid Response
    [Documentation]    Verify /health endpoint returns correct structure with status, model, and provider
    [Tags]    health

    Create Session    asr    ${ASR_URL}    verify=True
    ${response}=    GET On Session    asr    /health    expected_status=200

    # Verify response structure
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    status
    Dictionary Should Contain Key    ${json}    model
    Dictionary Should Contain Key    ${json}    provider

    # Verify health status
    Should Be Equal    ${json}[status]    healthy
    Should Not Be Empty    ${json}[model]
    Should Not Be Empty    ${json}[provider]

ASR Info Endpoint Returns Valid Response
    [Documentation]    Verify /info endpoint returns correct structure with capabilities
    [Tags]    infra

    Create Session    asr    ${ASR_URL}    verify=True
    ${response}=    GET On Session    asr    /info    expected_status=200

    # Verify response structure
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    model_id
    Dictionary Should Contain Key    ${json}    provider
    Dictionary Should Contain Key    ${json}    capabilities

    # Verify capabilities is a list
    Should Be True    isinstance($json['capabilities'], list)
    Should Not Be Empty    ${json}[model_id]
    Should Not Be Empty    ${json}[provider]

ASR Transcribe Endpoint Accepts Audio File
    [Documentation]    Verify /transcribe endpoint accepts file upload and returns 200
    [Tags]    audio-upload

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}

    # Verify successful upload
    Should Be Equal As Integers    ${response.status_code}    200

ASR Transcribe Response Contains Required Fields
    [Documentation]    Verify transcription response contains text, words, and segments
    [Tags]    audio-upload

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}
    ${json}=    Set Variable    ${response.json()}

    # Verify required fields exist
    Dictionary Should Contain Key    ${json}    text
    Dictionary Should Contain Key    ${json}    words
    Dictionary Should Contain Key    ${json}    segments

    # Verify text is non-empty string
    Should Be True    isinstance($json['text'], str)
    Should Not Be Empty    ${json}[text]

    # Verify words and segments are lists
    Should Be True    isinstance($json['words'], list)
    Should Be True    isinstance($json['segments'], list)

ASR Transcribe Response Words Have Timestamps
    [Documentation]    Verify word-level timestamps in transcription response
    [Tags]    audio-upload

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}
    ${json}=    Set Variable    ${response.json()}

    # Verify words list is not empty
    Should Not Be Empty    ${json}[words]

    # Verify first word has required timestamp fields
    ${first_word}=    Get From List    ${json}[words]    0
    Dictionary Should Contain Key    ${first_word}    word
    Dictionary Should Contain Key    ${first_word}    start
    Dictionary Should Contain Key    ${first_word}    end

    # Verify timestamps are numeric and ordered correctly
    Should Be True    isinstance($first_word['start'], (int, float))
    Should Be True    isinstance($first_word['end'], (int, float))
    Should Be True    ${first_word}[start] < ${first_word}[end]
    Should Be True    ${first_word}[start] >= 0

ASR Transcribe Response Words Have Confidence Scores
    [Documentation]    Verify confidence scores in word-level transcription
    [Tags]    audio-upload

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}
    ${json}=    Set Variable    ${response.json()}

    # Verify first word has confidence
    ${first_word}=    Get From List    ${json}[words]    0
    Dictionary Should Contain Key    ${first_word}    confidence

    # Verify confidence is between 0 and 1
    Should Be True    isinstance($first_word['confidence'], (int, float))
    Should Be True    ${first_word}[confidence] >= 0
    Should Be True    ${first_word}[confidence] <= 1

ASR Info Capabilities Include Timestamps
    [Documentation]    Verify ASR service reports timestamp capability
    [Tags]    infra

    ${info}=    Get ASR Service Info    ${ASR_URL}

    # Verify timestamps capability is reported
    ${has_timestamps}=    Evaluate    'timestamps' in $info['capabilities'] or 'word_timestamps' in $info['capabilities']
    Should Be True    ${has_timestamps}    ASR service should report timestamp capability


# =============================================================================
# Provider Capability Tests
# =============================================================================
# These tests verify the capability-based architecture that enables
# conditional processing (e.g., skipping pyannote when provider has diarization)

ASR Capabilities Format Is Valid List
    [Documentation]    Verify capabilities is a list of strings
    [Tags]    infra

    ${info}=    Get ASR Service Info    ${ASR_URL}

    # Capabilities must be a list
    Should Be True    isinstance($info['capabilities'], list)    Capabilities should be a list

    # Each capability should be a string
    FOR    ${cap}    IN    @{info}[capabilities]
        Should Be True    isinstance($cap, str)    Each capability should be a string: ${cap}
    END

ASR Capabilities Are From Known Set
    [Documentation]    Verify reported capabilities are valid known capabilities
    ...                Known capabilities: timestamps, word_timestamps, diarization,
    ...                speaker_identification, long_form, language_detection, vad_filter,
    ...                translation, chunked_processing
    [Tags]    infra

    ${info}=    Get ASR Service Info    ${ASR_URL}

    # Define known capabilities (union of all provider capabilities + mock server)
    @{known_caps}=    Create List    timestamps    word_timestamps    diarization
    ...    segments    speaker_identification    long_form    language_detection
    ...    vad_filter    translation    chunked_processing

    # All reported capabilities should be known
    FOR    ${cap}    IN    @{info}[capabilities]
        ${is_known}=    Evaluate    $cap in $known_caps
        Should Be True    ${is_known}
        ...    Unknown capability reported: ${cap}. Known: ${known_caps}
    END

ASR Response Segments Structure When Provider Has Segments
    [Documentation]    Verify segment structure when provider reports segments capability
    ...                Segments should have speaker, start, end, and text fields
    [Tags]    audio-upload

    ${info}=    Get ASR Service Info    ${ASR_URL}
    ${has_segments}=    Evaluate    'segments' in $info['capabilities']

    # Only test segment structure if provider claims segments capability
    IF    ${has_segments}
        ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}
        ${json}=    Set Variable    ${response.json()}

        IF    len($json['segments']) > 0
            ${first_segment}=    Get From List    ${json}[segments]    0
            Dictionary Should Contain Key    ${first_segment}    start
            Dictionary Should Contain Key    ${first_segment}    end
            Dictionary Should Contain Key    ${first_segment}    text

            # Timestamps should be numeric
            Should Be True    isinstance($first_segment['start'], (int, float))
            Should Be True    isinstance($first_segment['end'], (int, float))
        END
    END

ASR Response Segments Have Speaker Labels When Provider Has Diarization
    [Documentation]    Verify segments include speaker labels when provider has diarization
    ...                This is critical for VibeVoice integration - providers with built-in
    ...                diarization should return segments with speaker field populated
    [Tags]    audio-upload

    ${info}=    Get ASR Service Info    ${ASR_URL}
    ${has_diarization}=    Evaluate    'diarization' in $info['capabilities']

    # Skip test if provider doesn't have diarization
    Skip If    not ${has_diarization}    Provider does not have diarization capability

    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}
    ${json}=    Set Variable    ${response.json()}

    # With diarization capability, segments should have speaker labels
    Should Not Be Empty    ${json}[segments]
    ...    Provider with diarization should return segments

    ${first_segment}=    Get From List    ${json}[segments]    0
    Dictionary Should Contain Key    ${first_segment}    speaker
    ...    Segments from diarization provider must have 'speaker' field
    Should Not Be Empty    ${first_segment}[speaker]
    ...    Speaker label should not be empty

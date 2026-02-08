*** Settings ***
Documentation    VibeVoice Diarization Test - tests against existing VibeVoice service
...
...              Run this test when VibeVoice ASR service is already running on port 8767.
...              This test validates diarization capability without managing the service lifecycle.
...
...              Prerequisites:
...              - VibeVoice ASR service running on port 8767
...              - Start with: cd extras/asr-services && docker compose up vibevoice-asr -d
Library          RequestsLibrary
Library          Collections
Resource         ../resources/asr_keywords.robot

*** Variables ***
${VIBEVOICE_URL}     http://localhost:8767
${TEST_AUDIO_FILE}   ${CURDIR}/../test_assets/DIY_Experts_Glass_Blowing_16khz_mono_1min.wav

*** Test Cases ***

VibeVoice Service Is Healthy
    [Documentation]    Verify VibeVoice ASR service is running and healthy
    [Tags]    requires-gpu	health
    [Timeout]    30s

    Check ASR Service Health    ${VIBEVOICE_URL}
    Log    VibeVoice service is healthy

VibeVoice Reports Diarization Capability
    [Documentation]    Verify VibeVoice reports diarization in capabilities
    [Tags]    requires-gpu	infra
    [Timeout]    30s

    ${info}=    Get ASR Service Info    ${VIBEVOICE_URL}

    Log    Provider: ${info}[provider]
    Log    Model: ${info}[model_id]
    Log    Capabilities: ${info}[capabilities]

    # VibeVoice must report diarization capability
    ${has_diarization}=    Evaluate    'diarization' in $info['capabilities']
    Should Be True    ${has_diarization}
    ...    VibeVoice should report diarization capability, got: ${info}[capabilities]

VibeVoice Transcription Returns Diarized Segments
    [Documentation]    Upload 1-minute audio and verify diarized segments are returned
    [Tags]    requires-gpu	e2e
    [Timeout]    180s

    # Upload audio file
    ${response}=    Upload Audio For ASR Transcription    ${TEST_AUDIO_FILE}    ${VIBEVOICE_URL}
    Should Be Equal As Integers    ${response.status_code}    200
    ...    Transcription request failed with status ${response.status_code}

    ${json}=    Set Variable    ${response.json()}

    # Log the full transcript
    Log    Full transcript: ${json}[text]

    # Verify segments are present
    Dictionary Should Contain Key    ${json}    segments
    ${segments}=    Set Variable    ${json}[segments]
    Should Not Be Empty    ${segments}
    ...    VibeVoice should return segments with speaker diarization

    ${segment_count}=    Get Length    ${segments}
    Log    Found ${segment_count} diarized segments

    # Verify each segment has valid structure
    # Note: Non-speech segments (like [Music]) may have speaker=null
    ${speech_segments}=    Create List
    FOR    ${index}    ${segment}    IN ENUMERATE    @{segments}
        # Required fields
        Dictionary Should Contain Key    ${segment}    speaker
        Dictionary Should Contain Key    ${segment}    start
        Dictionary Should Contain Key    ${segment}    end
        Dictionary Should Contain Key    ${segment}    text

        # Timestamps must be valid
        Should Be True    ${segment}[start] >= 0
        Should Be True    ${segment}[end] > ${segment}[start]

        # Track segments with speaker labels (speech segments)
        ${has_speaker}=    Evaluate    $segment['speaker'] is not None
        IF    ${has_speaker}
            Append To List    ${speech_segments}    ${segment}
            Log    [${segment}[speaker]] ${segment}[start]s-${segment}[end]s: ${segment}[text]
        ELSE
            Log    [non-speech] ${segment}[start]s-${segment}[end]s: ${segment}[text]
        END
    END

    # Verify we have at least some speech segments with speaker labels
    ${speech_count}=    Get Length    ${speech_segments}
    Should Be True    ${speech_count} > 0
    ...    Expected at least one segment with speaker label, got ${speech_count}

    # Verify segments cover reasonable duration for 1-min audio
    ${last_segment}=    Get From List    ${segments}    -1
    ${total_duration}=    Set Variable    ${last_segment}[end]

    Should Be True    ${total_duration} > 30
    ...    Segments only cover ${total_duration}s, expected > 30s for 1-min audio
    Should Be True    ${total_duration} < 90
    ...    Segments cover ${total_duration}s, expected < 90s for 1-min audio

    Log    Total diarization coverage: ${total_duration} seconds
    Log    Number of speaker segments: ${segment_count}

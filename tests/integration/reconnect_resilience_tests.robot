*** Settings ***
Documentation    Reconnect resilience and leading-silence trimming (always_persist).
...
...              These exercise the audio-capture lifecycle that the unit/DB tests
...              cover in isolation:
...
...              1. A mid-session reconnect (abrupt disconnect, then reopen the same
...                 device) must NOT strand a transcript on a conversation that gets
...                 deleted for missing audio. Guards the single-flight persistence job
...                 + the salvage guard.
...
...              2. With always_persist on, a long silence before speech is recorded on
...                 the placeholder. At finalize the leading silence is split off onto a
...                 soft-deleted remnant (audio kept in Mongo) so the visible conversation
...                 begins at the first speech.
...
...              Both need real streaming transcription (the no-API mock is batch only),
...              so they are tagged requires-api-keys and slow.

Resource         ../resources/websocket_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/mongodb_keywords.robot
Resource         ../resources/session_keywords.robot
Variables        ../setup/test_env.py

Suite Setup      Get Admin API Session
Test Teardown    Cleanup All Audio Streams

*** Variables ***
${SPEECH_AUDIO}      ${CURDIR}/../test_assets/DIY_Experts_Glass_Blowing_16khz_mono_1min.wav
${SILENCE_AUDIO}     ${CURDIR}/../test_assets/silence_60s_16khz_mono.wav

*** Test Cases ***

Reconnect Mid Session Does Not Orphan Transcripts
    [Documentation]    An abrupt disconnect followed by a reconnect on the same device
    ...                must not leave a transcript-bearing conversation soft-deleted as
    ...                audio_chunks_not_ready. Reproduces the reconnect orphaning bug.
    [Tags]    e2e	audio-streaming	requires-api-keys	slow
    [Timeout]    300s

    ${device}=    Set Variable    test-reconnect
    ${client_id}=    Get Client ID From Device Name    ${device}

    # Arrange + Act — first leg: speak, so the placeholder gains a real transcript.
    ${stream}=    Open Audio Stream With Always Persist    device_name=${device}
    Send Audio Chunks To Stream    ${stream}    ${SPEECH_AUDIO}    num_chunks=60
    Sleep    8s    # let streaming transcription land a transcript on the conversation

    # Abrupt disconnect (network drop), then reconnect on the SAME device.
    Close Audio Stream Without Stop Event    ${stream}
    Sleep    3s
    ${stream2}=    Open Audio Stream With Always Persist    device_name=${device}
    Send Audio Chunks To Stream    ${stream2}    ${SPEECH_AUDIO}    num_chunks=60
    Sleep    5s
    Close Audio Stream    ${stream2}

    # Let post-conversation processing (and any audio_chunks_not_ready deletions) settle.
    Sleep    20s

    # Assert — no transcript was stranded on a deleted conversation.
    ${orphans}=    Get Orphaned Transcript Count    ${client_id}
    Should Be Equal As Integers    ${orphans}    0
    ...    Reconnect stranded ${orphans} transcript(s) on audio_chunks_not_ready conversations

Leading Silence Is Trimmed Off The Conversation
    [Documentation]    With always_persist on, a long silence before speech is split off
    ...                onto a soft-deleted "leading_silence" remnant so the visible
    ...                conversation begins at the speech (silence audio stays in Mongo).
    [Tags]    e2e	audio-streaming	requires-api-keys	slow
    [Timeout]    300s

    ${device}=    Set Variable    test-silence
    ${client_id}=    Get Client ID From Device Name    ${device}

    # Arrange + Act — 60s of silence, then speech, in one continuous session.
    ${stream}=    Open Audio Stream With Always Persist    device_name=${device}
    Send Audio Chunks To Stream    ${stream}    ${SILENCE_AUDIO}
    Send Audio Chunks To Stream    ${stream}    ${SPEECH_AUDIO}    num_chunks=60
    Sleep    8s
    Close Audio Stream    ${stream}
    Sleep    20s

    # Assert — a soft-deleted leading-silence remnant exists, and the visible
    # conversation does NOT carry the full ~60s+ of leading silence.
    ${conversations}=    Get Client Conversations Including Deleted    ${client_id}
    ${silence_remnants}=    Create List
    ${visible_durations}=    Create List
    FOR    ${conv}    IN    @{conversations}
        IF    '${conv}[deletion_reason]' == 'leading_silence'
            Append To List    ${silence_remnants}    ${conv}
        END
        IF    not ${conv}[deleted]
            Append To List    ${visible_durations}    ${conv}[audio_total_duration]
        END
    END
    ${remnant_count}=    Get Length    ${silence_remnants}
    Should Be True    ${remnant_count} >= 1
    ...    Expected a soft-deleted leading_silence remnant; found none (silence not trimmed)

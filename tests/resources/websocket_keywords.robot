*** Settings ***
Documentation    WebSocket audio streaming keywords using the shared AudioStreamClient
Library          Collections
Library          OperatingSystem
Library          String
Library          ../libs/audio_stream_library.py
Library          ../libs/auth_helpers.py
Variables        ../setup/test_env.py
Resource         session_keywords.robot
Resource         queue_keywords.robot

*** Keywords ***
Get Client ID From Device Name
    [Documentation]    Construct client_id from device_name for test admin user
    ...                Format: {last_6_chars_of_user_id}-{first_10_chars_of_device_name}
    ...                Matches backend logic in client_manager.py:generate_client_id()
    [Arguments]    ${device_name}

    # Get admin user ID dynamically from JWT token (changes on each database reset)
    ${admin_session}=    Get Admin API Session
    ${token}=    Get Authentication Token    ${admin_session}    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}
    ${user_id}=    Get User ID From Token    ${token}

    # Extract last 6 characters of user ID (matches backend logic)
    ${user_suffix}=    Get Substring    ${user_id}    -6

    # Sanitize and truncate device name to 10 chars (matches backend: [:10])
    # Backend sanitizes: lowercase, alphanumeric + hyphens only
    ${device_lower}=    Convert To Lower Case    ${device_name}
    ${device_truncated}=    Get Substring    ${device_lower}    0    10

    ${client_id}=    Set Variable    ${user_suffix}-${device_truncated}
    RETURN    ${client_id}


Stream Audio File Via WebSocket
    [Documentation]    Stream a WAV file via WebSocket using Wyoming protocol
    ...                Uses the shared AudioStreamClient from advanced_omi_backend.clients
    [Arguments]    ${audio_file_path}    ${device_name}=robot-test    ${recording_mode}=streaming

    File Should Exist    ${audio_file_path}

    # Get a fresh token for WebSocket auth
    ${token}=    Get Authentication Token    api    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}

    ${chunks_sent}=    Stream Audio File
    ...    base_url=${API_URL}
    ...    token=${token}
    ...    wav_path=${audio_file_path}
    ...    device_name=${device_name}
    ...    recording_mode=${recording_mode}

    Log    Streamed ${chunks_sent} audio chunks via WebSocket
    Should Be True    ${chunks_sent} > 0    No audio chunks were sent
    RETURN    ${chunks_sent}

Stream Audio File Batch Mode
    [Documentation]    Stream a WAV file in batch mode via WebSocket
    [Arguments]    ${audio_file_path}    ${device_name}=robot-test

    ${chunks_sent}=    Stream Audio File Via WebSocket
    ...    ${audio_file_path}
    ...    ${device_name}
    ...    recording_mode=batch

    RETURN    ${chunks_sent}

Stream Audio File Streaming Mode
    [Documentation]    Stream a WAV file in streaming mode via WebSocket
    [Arguments]    ${audio_file_path}    ${device_name}=robot-test

    ${chunks_sent}=    Stream Audio File Via WebSocket
    ...    ${audio_file_path}
    ...    ${device_name}
    ...    recording_mode=streaming

    RETURN    ${chunks_sent}

# =============================================================================
# Non-blocking streaming keywords (for testing during stream)
# =============================================================================

Open Audio Stream
    [Documentation]    Start a WebSocket audio stream (non-blocking)
    ...                Returns immediately after connection. Use Send Audio Chunks
    ...                to send audio, and Close Audio Stream to close.
    [Arguments]    ${device_name}=robot-test    ${recording_mode}=streaming

    ${token}=    Get Authentication Token    api    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}

    # Call the Python library method directly
    ${stream_id}=    Start Audio Stream
    ...    base_url=${API_URL}
    ...    token=${token}
    ...    device_name=${device_name}
    ...    recording_mode=${recording_mode}

    Log    Started audio stream ${stream_id} for device ${device_name}
    RETURN    ${stream_id}

Open Audio Stream With Always Persist
    [Documentation]    Start a WebSocket audio stream with always_persist enabled.
    ...                always_persist is a backend-level setting (not per-session).
    ...                This keyword ensures the setting is enabled before opening the stream.
    ...                Returns stream_id for sending chunks.
    [Arguments]    ${device_name}=robot-test    ${recording_mode}=streaming

    # Open a regular stream - always_persist is read from backend config at enqueue time
    ${stream_id}=    Open Audio Stream    device_name=${device_name}    recording_mode=${recording_mode}

    Log    Started audio stream ${stream_id} (always_persist is a backend setting)
    RETURN    ${stream_id}

Stream Audio File With Always Persist
    [Documentation]    Stream a WAV file via WebSocket with always_persist enabled.
    ...                always_persist is a backend-level setting (not per-session).
    ...                Caller should ensure the setting is enabled via API before calling.
    [Arguments]    ${audio_file_path}    ${device_name}=robot-test    ${recording_mode}=streaming

    # Stream normally - always_persist is read from backend config at enqueue time
    ${chunks_sent}=    Stream Audio File Via WebSocket    ${audio_file_path}    device_name=${device_name}    recording_mode=${recording_mode}

    Log    Streamed ${chunks_sent} chunks (always_persist is a backend setting)
    Should Be True    ${chunks_sent} > 0
    RETURN    ${chunks_sent}

Send Audio Chunks To Stream
    [Documentation]    Send audio chunks from a file to an open stream
    [Arguments]    ${stream_id}    ${audio_file_path}    ${num_chunks}=${None}    ${realtime_pacing}=False

    File Should Exist    ${audio_file_path}

    # Call the Python library method directly
    ${chunks_sent}=    Send Audio Chunks
    ...    stream_id=${stream_id}
    ...    wav_path=${audio_file_path}
    ...    num_chunks=${num_chunks}
    ...    realtime_pacing=${realtime_pacing}

    Log    Sent ${chunks_sent} chunks to stream ${stream_id}
    RETURN    ${chunks_sent}

Send Audio Stop Event
    [Documentation]    Send audio-stop event without closing the WebSocket
    ...                This simulates a user manually stopping recording
    [Arguments]    ${stream_id}

    # Call the Python library method directly
    Send Audio Stop Event    ${stream_id}
    Log    Sent audio-stop event to stream ${stream_id}

Close Audio Stream
    [Documentation]    Stop an audio stream and close the connection
    [Arguments]    ${stream_id}

    # Call the Python library method directly
    ${total_chunks}=    Stop Audio Stream    ${stream_id}
    Log    Stopped stream ${stream_id}, total chunks: ${total_chunks}
    RETURN    ${total_chunks}

Close Audio Stream Without Stop Event
    [Documentation]    Close WebSocket connection without sending audio-stop event.
    ...                This simulates abrupt disconnection (network failure, client crash)
    ...                and should trigger websocket_disconnect end_reason.
    [Arguments]    ${stream_id}

    # Call the Python library method directly
    ${total_chunks}=    Close Audio Stream Without Stop    ${stream_id}
    Log    Closed stream ${stream_id} abruptly (no audio-stop), total chunks: ${total_chunks}
    RETURN    ${total_chunks}

Cleanup All Audio Streams
    [Documentation]    Stop all active streams (use in teardown)
    Cleanup All Streams

Stream And Wait For Conversation
    [Documentation]    Send audio chunks to stream, wait for conversation to be created and closed.
    ...                Returns the conversation_id of the finished conversation.
    ...                Works correctly even with existing conversations by tracking new conversation creation.
    [Arguments]    ${stream_id}    ${audio_file_path}    ${device_name}    ${num_chunks}=100

    # Construct client_id from device_name for job lookups
    ${client_id}=    Get Client ID From Device Name    ${device_name}

    # Get baseline conversation IDs before streaming to detect new conversation
    ${baseline_jobs}=    Get Jobs By Type And Client    open_conversation    ${client_id}
    ${existing_conv_ids}=    Create List
    FOR    ${job}    IN    @{baseline_jobs}
        ${meta}=    Set Variable    ${job}[meta]
        ${conv_id}=    Evaluate    $meta.get('conversation_id', '')
        IF    '${conv_id}' != ''
            Append To List    ${existing_conv_ids}    ${conv_id}
        END
    END
    Log    Baseline conversation IDs: ${existing_conv_ids}

    # Send audio chunks
    Send Audio Chunks To Stream    ${stream_id}    ${audio_file_path}    num_chunks=${num_chunks}

    # Wait for NEW conversation job to be created (not in baseline)
    ${new_job}=    Wait Until Keyword Succeeds    60s    3s
    ...    Wait For New Conversation Job    open_conversation    ${client_id}    ${existing_conv_ids}

    ${conv_meta}=    Set Variable    ${new_job}[meta]
    ${conversation_id}=    Evaluate    $conv_meta.get('conversation_id', '')
    Log    New conversation created: ${conversation_id}

    # Wait for conversation to close via inactivity timeout (with queue drain, can take 45+ seconds)
    Wait For Job Status    ${new_job}[job_id]    finished    timeout=60s    interval=2s
    Log    Conversation closed: ${conversation_id}

    RETURN    ${conversation_id}

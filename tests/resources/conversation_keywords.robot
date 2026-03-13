*** Settings ***
Documentation    Conversation Management Keywords
Library          RequestsLibrary
Library          Collections
Library          Process
Library          String
Resource         session_keywords.robot
Resource         audio_keywords.robot


*** Keywords ***

Get User Conversations
    [Documentation]    Get conversations for authenticated user (uses admin session)

    ${response}=    GET On Session    api    /api/conversations    expected_status=200
    RETURN    ${response.json()}[conversations]

Get Conversations By Client ID
    [Documentation]    Get conversations filtered by client_id
    ...                Returns only conversations matching the specified client_id
    [Arguments]    ${client_id}

    ${all_conversations}=    Get User Conversations
    ${filtered}=    Create List

    FOR    ${conv}    IN    @{all_conversations}
        ${conv_client_id}=    Set Variable    ${conv}[client_id]
        IF    '${conv_client_id}' == '${client_id}'
            Append To List    ${filtered}    ${conv}
        END
    END

    RETURN    ${filtered}

Wait For Conversation By Client ID
    [Documentation]    Wait for at least one conversation to exist for the given client_id.
    ...                Polls until a conversation is found or timeout is reached.
    ...                Returns the list of conversations for that client.
    [Arguments]    ${client_id}    ${expected_count}=1

    ${conversations}=    Get Conversations By Client ID    ${client_id}
    ${count}=    Get Length    ${conversations}

    Should Be True    ${count} >= ${expected_count}
    ...    Expected at least ${expected_count} conversation(s) for client ${client_id}, found ${count}

    RETURN    ${conversations}

Get Conversation By ID
    [Documentation]    Get a specific conversation by ID
    [Arguments]       ${conversation_id}
    ${response}=    GET On Session    api    /api/conversations/${conversation_id}
    RETURN    ${response.json()}[conversation]

Get Conversation Versions
    [Documentation]    Get version history for a conversation
    [Arguments]    ${conversation_id}
    ${response}=    GET On Session    api    /api/conversations/${conversation_id}/versions
    RETURN    ${response.json()}[transcript_versions]

Get conversation memory versions
    [Documentation]    Get memory version history for a conversation
    [Arguments]    ${conversation_id}
    ${response}=    GET On Session    api    /api/conversations/${conversation_id}/versions/memory
    RETURN    ${response.json()}[memory_versions]

Reprocess Transcript
    [Documentation]    Trigger transcript reprocessing for a conversation
    [Arguments]     ${conversation_id}

    ${response}=    POST On Session    api    /api/conversations/${conversation_id}/reprocess-transcript    expected_status=200

    ${reprocess_data}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${reprocess_data}    job_id
    Dictionary Should Contain Key    ${reprocess_data}    status

    ${job_id}=    Set Variable    ${reprocess_data}[job_id]
    ${initial_status}=    Set Variable    ${reprocess_data}[status]

    Log    Reprocess job created: ${job_id} with status: ${initial_status}    INFO
    Should Be True    '${initial_status}' in ['queued', 'started']    Status should be 'queued' or 'started', got: ${initial_status}

    RETURN    ${response.json()}

Reprocess Memory
    [Documentation]    Trigger memory reprocessing for a conversation
    [Arguments]    ${conversation_id}    ${transcript_version_id}=active
    &{params}=     Create Dictionary    transcript_version_id=${transcript_version_id}

    ${response}=    POST On Session    api    /api/conversations/${conversation_id}/reprocess-memory        params=${params}
    RETURN    ${response.json()}

Activate Transcript Version
    [Documentation]    Activate a specific transcript version
    [Arguments]    ${conversation_id}    ${version_id}

    ${response}=    POST On Session    api    /api/conversations/${conversation_id}/activate-transcript/${version_id}
    RETURN    ${response.json()}

Activate Memory Version
    [Documentation]    Activate a specific memory version
    [Arguments]     ${conversation_id}    ${version_id}

    ${response}=    POST On Session    api    /api/conversations/${conversation_id}/activate-memory/${version_id}
    RETURN    ${response.json()}

Delete Conversation
    [Documentation]    Delete a conversation
    [Arguments]     ${conversation_id}

    ${response}=    DELETE On Session    api    /api/conversations/${conversation_id}    headers=${headers}
    RETURN    ${response.json()}

Delete Conversation Version
    [Documentation]    Delete a specific version from a conversation
    [Arguments]     ${conversation_id}    ${version_type}    ${version_id}

    ${response}=    DELETE On Session    api    /api/conversations/${conversation_id}/versions/${version_type}/${version_id}    headers=${headers}
    RETURN    ${response.json()}

Close Current Conversation
    [Documentation]    Close the current conversation for a client
    [Arguments]    ${client_id}

    ${response}=    POST On Session    api    /api/conversations/${client_id}/close    headers=${headers}
    RETURN    ${response.json()}

Add Speaker To Conversation
    [Documentation]    Add a speaker to the speakers_identified list
    [Arguments]    ${conversation_id}    ${speaker_id}
    &{params}=     Create Dictionary    speaker_id=${speaker_id}

    ${response}=    POST On Session    api    /api/conversations/${conversation_id}/speakers    headers=${headers}    params=${params}
    RETURN    ${response.json()}

Update Transcript Segment
    [Documentation]    Update a specific transcript segment
    [Arguments]    ${conversation_id}    ${segment_index}    ${speaker_id}=${None}    ${start_time}=${None}    ${end_time}=${None}
    &{params}=     Create Dictionary

    IF    '${speaker_id}' != '${None}'
        Set To Dictionary    ${params}    speaker_id=${speaker_id}
    END
    IF    '${start_time}' != '${None}'
        Set To Dictionary    ${params}    start_time=${start_time}
    END
    IF    '${end_time}' != '${None}'
        Set To Dictionary    ${params}    end_time=${end_time}
    END

    ${response}=    PUT On Session    api    /api/conversations/${conversation_id}/transcript/${segment_index}    headers=${headers}    params=${params}
    RETURN    ${response.json()}


Create Test Conversation
    [Documentation]    Create a test conversation by started a test audio file
    [Arguments]     ${device_name}=test-device

    # Upload test audio file to create a conversation

    ${conversation}=    Upload Audio File     ${TEST_AUDIO_FILE}    ${device_name}

    RETURN    ${conversation}


Find Test Conversation
    [Documentation]    Find the oldest (earliest created) conversation or create one if none exist
    ...                Returns the first conversation in the list, which should be the oldest/fixture
    ${conversations_data}=    Get User Conversations
    Log    Retrieved conversations data: ${conversations_data}

    # conversations_data is now a flat list
    ${count}=    Get Length    ${conversations_data}

    IF    ${count} > 0
        # Sort by created_at to get oldest conversation first (most stable for tests)
        ${sorted_convs}=    Evaluate    sorted($conversations_data, key=lambda x: x.get('created_at', ''))
        ${oldest_conv}=    Set Variable    ${sorted_convs}[0]
        Log    Using oldest conversation (created_at: ${oldest_conv}[created_at])
        RETURN    ${oldest_conv}
    END

    # If no conversations exist, create one by uploading test audio
    Log    No conversations found, creating one by uploading test audio
    ${conversation}=    Upload Audio File    ${TEST_AUDIO_FILE}    ${TEST_DEVICE_NAME}

    # Wait for initial started to complete
    Sleep    5s

    RETURN    ${conversation}

Check Conversation Has End Reason
    [Documentation]    Check if conversation has end_reason set (not None)
    [Arguments]    ${conversation_id}

    ${conversation}=    Get Conversation By ID    ${conversation_id}
    ${end_reason}=    Set Variable    ${conversation}[end_reason]
    Should Not Be Equal As Strings    ${end_reason}    None    msg=End reason not set yet
    RETURN    ${conversation}

Conversation Should Have End Reason
    [Documentation]    Verify conversation has specific end_reason value
    ...
    ...    This keyword checks if the conversation's end_reason field matches the expected value.
    [Arguments]    ${conversation_id}    ${expected_end_reason}

    ${conversation}=    Get Conversation By ID    ${conversation_id}
    ${actual_end_reason}=    Set Variable    ${conversation}[end_reason]
    Should Be Equal As Strings    ${actual_end_reason}    ${expected_end_reason}
    ...    msg=Expected end_reason '${expected_end_reason}', got '${actual_end_reason}'

Verify Conversation Processing Status
    [Documentation]    Verify conversation has expected processing_status value
    [Arguments]    ${conversation_id}    ${expected_status}

    ${conversation}=    Get Conversation By ID    ${conversation_id}

    Should Contain    ${conversation}    processing_status
    Should Be Equal As Strings    ${conversation}[processing_status]    ${expected_status}
    ...    Expected processing_status='${expected_status}', got '${conversation}[processing_status]'

    Log    ✅ Conversation ${conversation_id} has processing_status='${expected_status}'

Verify Conversation Always Persist Flag
    [Documentation]    Verify conversation has always_persist=True
    [Arguments]    ${conversation_id}

    ${conversation}=    Get Conversation By ID    ${conversation_id}

    Should Contain    ${conversation}    always_persist
    Should Be True    ${conversation}[always_persist]
    ...    Expected always_persist=True, got ${conversation}[always_persist]

    Log    ✅ Conversation ${conversation_id} has always_persist=True

Star Conversation
    [Documentation]    Toggle the starred status of a conversation
    [Arguments]    ${conversation_id}

    ${response}=    POST On Session    api    /api/conversations/${conversation_id}/star    expected_status=200
    RETURN    ${response.json()}

Get Starred Conversations
    [Documentation]    Get only starred/favorited conversations
    &{params}=    Create Dictionary    starred_only=true
    ${response}=    GET On Session    api    /api/conversations    params=${params}    expected_status=200
    RETURN    ${response.json()}[conversations]

Verify Placeholder Conversation Title
    [Documentation]    Verify conversation has placeholder title
    [Arguments]    ${conversation_id}

    ${conversation}=    Get Conversation By ID    ${conversation_id}

    # Placeholder title can be either "Processing..." or "Transcription Failed"
    ${title}=    Set Variable    ${conversation}[title]
    ${has_processing}=    Run Keyword And Return Status    Should Contain    ${title}    Processing
    ${has_failed}=    Run Keyword And Return Status    Should Contain    ${title}    Transcription Failed

    ${is_placeholder}=    Evaluate    ${has_processing} or ${has_failed}

    Should Be True    ${is_placeholder}
    ...    Expected placeholder title, got: ${title}

    Log    ✅ Conversation has placeholder title: ${title}

*** Settings ***
Documentation    Conversation Management API Tests
Library          RequestsLibrary
Library          Collections
Library          String
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/user_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/queue_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

*** Test Cases ***

Get User Conversations Test
    [Documentation]    Test getting conversations for authenticated user
    [Tags]    conversation	permissions

    ${conversations_data}=        Get User Conversations

    # Verify conversation structure if any exist
   
    IF    isinstance($conversations_data, dict) and len($conversations_data) > 0
        ${client_ids}=    Get Dictionary Keys    ${conversations_data}
        FOR    ${client_id}    IN    @{client_ids}
            ${client_conversations}=    Set Variable    ${conversations_data}[${client_id}]
            FOR    ${conversation}    IN    @{client_conversations}
                # Verify conversation structure
                Dictionary Should Contain Key    ${conversation}    conversation_id
                Dictionary Should Contain Key    ${conversation}    created_at
            END
        END
    END

Get Conversation By ID Test
    [Documentation]    Test getting a specific conversation by ID
    [Tags]    conversation

    ${test_conversation}=    Find Test Conversation

    ${conversation_id}=    Set Variable    ${test_conversation}[conversation_id]
    ${conversation}=           Get Conversation By ID       ${conversation_id}

    # Verify conversation structure
    Dictionary Should Contain Key    ${conversation}    conversation_id
    Dictionary Should Contain Key    ${conversation}    created_at
    Should Be Equal    ${conversation}[conversation_id]    ${conversation_id}

Reprocess test and get Conversation Versions Test
    [Documentation]    Test getting version history for a conversation
    [Tags]    conversation

    ${test_conversation}=    Find Test Conversation
    ${conversation_id}=    Set Variable    ${test_conversation}[conversation_id]
    ${start_num_versions}=    Set Variable           ${test_conversation}[transcript_version_count]
    ${reprocess}=    Reprocess Transcript     ${conversation_id}

    # Wait for the reprocess job to complete before getting versions
    ${job_id}=    Set Variable    ${reprocess}[job_id]
    Wait For Job Status    ${job_id}    completed    timeout=120s    interval=5s

    ${conversation}=           Get Conversation By ID       ${conversation_id}
    ${updated_versions}=           Get Conversation Versions     ${conversation_id}

    ${expected_count}=    Evaluate    ${start_num_versions} + 1
    Should Be Equal As Integers     ${conversation}[transcript_version_count]    ${expected_count}
    Should be equal as strings     ${conversation}[active_transcript_version]      ${updated_versions}[-1][version_id]    


Unauthorized Conversation Access Test
    [Documentation]    Test that conversation endpoints require authentication
    [Tags]    conversation	permissions
    Get Anonymous Session    session

    # Try to access conversations without token
    ${response}=    GET On Session    session    /api/conversations    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401

Non-Existent Conversation Test
    [Documentation]    Test accessing a non-existent conversation
    [Tags]    conversation

    ${fake_id}=         Set Variable    non-existent-conversation-id

    ${response}=        GET On Session    api    /api/conversations/${fake_id}    expected_status=404
    Should Be Equal As Integers    ${response.status_code}    404

Reprocess Memory Test
    [Documentation]    Test triggering memory reprocessing and verify new version created
    [Tags]    conversation	memory

    Skip  msg=Memory reprocessing needs a different transcript and different test data to work

    ${test_conversation}=    Find Test Conversation

    ${conversation_id}=    Set Variable    ${test_conversation}[conversation_id]

    # Get initial memory version count
    ${initial_memory_count}=    Set Variable    ${test_conversation}[memory_version_count]
    Log    Initial memory version count: ${initial_memory_count}

    # Trigger memory reprocessing
    ${response}=    Reprocess Memory    ${conversation_id}

    # Verify response structure
    Dictionary Should Contain Key    ${response}    job_id
    Dictionary Should Contain Key    ${response}    status
    Should Be Equal As Strings    ${response}[status]    queued

    # Wait for job to complete
    ${job_id}=    Set Variable    ${response}[job_id]
    Wait For Job Status    ${job_id}    completed    timeout=60s    interval=5s

    # Verify new memory version was created
    ${updated_conversation}=    Get Conversation By ID    ${conversation_id}
    ${new_memory_count}=    Set Variable    ${updated_conversation}[memory_version_count]
    Log    New memory version count: ${new_memory_count}

    Should Be True    ${new_memory_count} > ${initial_memory_count}    Expected memory version count to increase

    ${memory_versions}=    Get conversation memory versions    ${conversation_id}
    Length Should Be    ${memory_versions}    ${new_memory_count}
  

Close Conversation Test
    [Documentation]    Test closing current conversation for a client
    [Tags]    conversation
    
    Skip     msg=Close conversation needs to be evaluated as to it's purpose from the client side

    Get Anonymous Session    anon_session

    Create API Session    admin_session
    ${client_id}=       Set Variable    test-client-${RANDOM_ID}

    # This might return 404 if client doesn't exist, which is expected
    ${response}=        POST On Session    admin_session    /api/conversations/${client_id}/close    expected_status=any
    Should Be True     ${response.status_code} in [200, 404]

Invalid Conversation Operations Test
    [Documentation]    Test invalid operations on conversations
    [Tags]    conversation

    ${fake_id}=         Set Variable    invalid-conversation-id

    # Test reprocessing non-existent conversation
    ${response}=        POST On Session    api    /api/conversations/${fake_id}/reprocess-transcript    expected_status=404
    Should Be Equal As Integers    ${response.status_code}    404

    # Test getting versions of non-existent conversation
    ${response}=        GET On Session    api    /api/conversations/${fake_id}/versions    expected_status=404
    Should Be Equal As Integers    ${response.status_code}    404

Transcript Version activate Test
    [Documentation]    Test version activation using oldest conversation for stability
    [Tags]    conversation

    # Find Test Conversation now returns the oldest conversation (most stable)
    ${test_conversation}=    Find Test Conversation
    ${conversation_id}=    Set Variable    ${test_conversation}[conversation_id]

    # Small delay to let any ongoing jobs from previous tests complete
    Sleep    2s

    ${versions}=    Get Conversation Versions    ${conversation_id}

    # Ensure we have at least 2 versions by reprocessing if needed
    IF     len(${versions}) < 2
        ${reprocess}=    Reprocess Transcript     ${conversation_id}
        # Wait for the reprocess job to complete before getting versions
        ${job_id}=    Set Variable    ${reprocess}[job_id]
        Wait For Job Status    ${job_id}    completed    timeout=120s    interval=5s
    END

    # Get fresh version list after reprocessing
    ${versions}=  Get Conversation Versions     ${conversation_id}
    Should Be True    len(${versions}) >= 2    msg=Should have at least 2 versions after reprocessing

    # Test activating a different version (activate version index 1)
    ${target_version}=    Set Variable    ${versions}[1][version_id]
    ${response}=       Activate Transcript Version      ${conversation_id}    ${target_version}
    Should Be Equal As Strings    ${response}[active_transcript_version]   ${target_version}  


        # ${active_memory}=     Get memory versions  
        # ...    ${test}[active_memory_version]
        # IF    '${active_memory}' != '${None}' and '${active_memory}' != 'null'
        #     ${response}=       Activate Memory Version       ${conversation_id}    ${active_memory}
        #     Should Be Equal As Integers    ${response.status_code}    200
        # END

Get conversation permission Test
    [Documentation]    Test that users can only access their own conversations
    [Tags]    conversation	permissions

    Create Test Conversation
    # Create a test user
    ${test_user}=       Create Test User    api
    Create API Session    user_session    email=${test_user}[email]    password=${TEST_USER_PASSWORD}

    # Get admin conversations
    ${admin_conversations}=    Get User Conversations
    Should Be True    len(${admin_conversations}) > 0

    # Get user conversations (should be empty for new user)
    ${user_conversations}=    GET On Session    user_session    /api/conversations
    Should Be Equal As Integers    ${user_conversations.status_code}    200
    ${user_conv_data}=    Set Variable    ${user_conversations.json()}
    Length Should Be    ${user_conv_data}[conversations]    0

    # Cleanup
    Delete User    api    ${test_user}[id]


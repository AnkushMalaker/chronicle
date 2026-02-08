*** Settings ***
Documentation    Memory Management API Tests
Library          RequestsLibrary
Library          Collections
Library          String
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/user_keywords.robot
Resource         ../resources/memory_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup
*** Test Cases ***

Get User Memories Test
    [Documentation]    Test getting memories for authenticated user and verify trumpet flower memory exists if memories are present
    [Tags]    memory	permissions	requires-api-keys

    ${response}=       GET On Session    api    /api/memories

    Should Be Equal As Integers    ${response.status_code}    200


    # Verify memory structure if any exist
    ${memories}=    Set Variable    ${response.json()}[memories]
    ${memory_count}=    Get Length    ${memories}

    # Only check for trumpet flower if memories exist
    IF    ${memory_count} > 0
        ${found_trumpet_flower}=    Set Variable    ${False}

        FOR    ${memory}    IN    @{memories}
            # Verify memory structure
            ${metadata}=     Set Variable    ${memory}[metadata]
            Dictionary Should Contain Key    ${memory}    id
            Dictionary Should Contain Key    ${memory}    memory
            Dictionary Should Contain Key    ${memory}    created_at
            Dictionary Should Contain Key    ${memory}    updated_at
            Dictionary Should Contain Key    ${metadata}    source
            Dictionary Should Contain Key    ${metadata}    client_id
            Dictionary Should Contain Key    ${metadata}    source_id
            Dictionary Should Contain Key    ${metadata}    user_id
            Dictionary Should Contain Key    ${metadata}    user_email

            # Verify timestamps are valid (not "Invalid Date", not empty)
            Should Not Be Equal    ${memory}[created_at]    ${EMPTY}    created_at should not be empty
            Should Not Be Equal    ${memory}[updated_at]    ${EMPTY}    updated_at should not be empty
            Should Not Be Equal    ${memory}[created_at]    Invalid Date    created_at should not be "Invalid Date"
            Should Not Be Equal    ${memory}[updated_at]    Invalid Date    updated_at should not be "Invalid Date"

            # Verify timestamps are numeric strings (Unix timestamps)
            ${created_timestamp}=    Convert To Integer    ${memory}[created_at]
            ${updated_timestamp}=    Convert To Integer    ${memory}[updated_at]
            Should Be True    ${created_timestamp} > 0    created_at should be a positive timestamp
            Should Be True    ${updated_timestamp} > 0    updated_at should be a positive timestamp

            # Check if memory contains "trumpet flower"
            ${memory_text}=    Convert To String    ${memory}[memory]
            ${contains}=    Run Keyword And Return Status    Should Contain    ${memory_text}    trumpet flower    ignore_case=True
            IF    ${contains}
                ${found_trumpet_flower}=    Set Variable    ${True}
                Log    Found memory containing "trumpet flower": ${memory_text}
            END
        END

        # Assert that we found at least one memory containing "trumpet flower"
        Should Be True    ${found_trumpet_flower}    No memory found containing "trumpet flower" (${memory_count} memories checked)
    ELSE
        Log    No memories found - skipping trumpet flower check (run integration test first to populate memories)
    END

Search Memories Test
    [Documentation]    Test searching memories by query and verify trumpet flower memory exists
    [Tags]    memory	requires-api-keys

    &{params}=         Create Dictionary    query=trumpet flower    limit=20    score_threshold=0.4
    ${response}=       GET On Session    api    /api/memories/search    params=${params}

    Should Be Equal As Integers    ${response.status_code}    200

    # Verify search results structure and find trumpet flower
    ${results}=    Set Variable    ${response.json()}[results]
    ${result_count}=    Get Length    ${results}

    # Only check for trumpet flower if results exist
    IF    ${result_count} > 0
        ${found_trumpet_flower}=    Set Variable    ${False}

        FOR    ${memory}    IN    @{results}
            # Check if memory contains "trumpet flower"
            ${memory_text}=    Convert To String    ${memory}[memory]
            ${contains}=    Run Keyword And Return Status    Should Contain    ${memory_text}    trumpet flower    ignore_case=True
            IF    ${contains}
                ${found_trumpet_flower}=    Set Variable    ${True}
                Log    Found memory containing "trumpet flower": ${memory_text}
            END
        END

        # Assert that we found at least one memory containing "trumpet flower"
        Should Be True    ${found_trumpet_flower}    No memory found containing "trumpet flower" (${result_count} search results checked)
    ELSE
        Log    No search results found - skipping trumpet flower check (run integration test first to populate memories)
    END

Memory Pagination Test
    [Documentation]    Test memory pagination with different limits
    [Tags]    memory	requires-api-keys

    # Test with small limit
    &{params1}=    Create Dictionary    limit=5
    ${response1}=  GET On Session    api    /api/memories    params=${params1}
    Should Be Equal As Integers    ${response1.status_code}    200
    ${memories1}=  Set Variable    ${response1.json()}
    ${count1}=     Get Length    ${memories1}
    Should Be True    ${count1} <= 5

    # Test with larger limit
    &{params2}=    Create Dictionary    limit=100
    ${response2}=  GET On Session    api    /api/memories    params=${params2}
    Should Be Equal As Integers    ${response2.status_code}    200
    ${memories2}=  Set Variable    ${response2.json()}
    ${count2}=     Get Length    ${memories2}

    # Second request should have >= first request count
    Should Be True    ${count2} >= ${count1}

Non-Admin Cannot Access Admin Memories Test
    [Documentation]    Test that non-admin users cannot access admin memory endpoint
    [Tags]    memory	permissions

    # Create a non-admin user
    ${test_user}=      Create Test User    api
    Create API Session    user_session    email=${test_user}[email]    password=${TEST_USER_PASSWORD}

    # Try to access admin memories endpoint
    ${response}=       GET On Session    user_session    /api/memories/admin    expected_status=403
    Should Be Equal As Integers    ${response.status_code}    403

    # Cleanup
    [Teardown]    Delete User    api    ${test_user}[id]

Unauthorized Memory Access Test
    [Documentation]    Test that memory endpoints require authentication
    [Tags]    memory	permissions
    Get Anonymous Session    session

    # Try to access memories without token
    ${response}=    GET On Session    session    /api/memories    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401

    # Try to search memories without token
    &{params}=     Create Dictionary    query=test
    ${response}=   GET On Session    session    /api/memories/search    params=${params}    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401


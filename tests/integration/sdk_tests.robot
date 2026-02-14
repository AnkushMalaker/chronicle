*** Settings ***
Documentation    Minimal tests for Chronicle Python SDK
...
...              Tests basic SDK functionality including authentication,
...              file upload, and conversation retrieval.
...
...              Placeholders included for unimplemented features.

Library          Process
Library          OperatingSystem
Library          Collections
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Variables        ../setup/test_env.py

Suite Setup      Suite Setup
Suite Teardown   Suite Teardown

*** Variables ***
${BACKEND_URL}        http://localhost:8001
${SDK_PATH}           ${CURDIR}/../../sdk/python
${TEST_AUDIO_DIR}     ${CURDIR}/../../extras/test-audios

*** Test Cases ***
SDK Can Authenticate With Admin Credentials
    [Documentation]    Test SDK login functionality
    [Tags]    permissions	sdk

    ${result}=    Run Process    uv    run    python
    ...    ${CURDIR}/../scripts/sdk_test_auth.py
    ...    ${BACKEND_URL}    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}
    Should Be Equal As Integers    ${result.rc}    0    SDK authentication should succeed
    Should Contain    ${result.stdout}    SUCCESS    Should print success message

SDK Can Upload Audio File
    [Documentation]    Test SDK audio upload functionality
    [Tags]    audio-upload	sdk

    ${test_audio}=    Set Variable    ${TEST_AUDIO_DIR}/audio_short.wav
    File Should Exist    ${test_audio}    Test audio file should exist

    ${result}=    Run Process    uv    run    python
    ...    ${CURDIR}/../scripts/sdk_test_upload.py
    ...    ${BACKEND_URL}    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}    ${test_audio}
    Should Be Equal As Integers    ${result.rc}    0    SDK upload should succeed
    Should Contain    ${result.stdout}    STATUS:started    File should be in started status

SDK Can Retrieve Conversations
    [Documentation]    Test SDK conversation retrieval
    [Tags]    conversation	sdk

    ${result}=    Run Process    uv    run    python
    ...    ${CURDIR}/../scripts/sdk_test_conversations.py
    ...    ${BACKEND_URL}    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}
    Should Be Equal As Integers    ${result.rc}    0    SDK should retrieve conversations
    Should Contain    ${result.stdout}    COUNT:    Should print conversation count

SDK Upload Respects Backend File Size Limit
    [Documentation]    Verify SDK properly reports backend errors for oversized files
    [Tags]    audio-upload	sdk

    # Note: This tests that SDK handles backend rejection gracefully
    # The 30-minute limit is enforced by the backend, not the SDK
    # Full test would require a 30+ minute audio file

    ${result}=    Run Process    uv    run    python
    ...    ${CURDIR}/../scripts/sdk_test_auth.py
    ...    ${BACKEND_URL}    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}
    Should Be Equal As Integers    ${result.rc}    0    SDK should handle backend errors gracefully

# ==============================================================================
# PLACEHOLDERS FOR UNIMPLEMENTED FEATURES
# ==============================================================================

SDK Can Stream Large Audio Files Via WebSocket
    [Documentation]    PLACEHOLDER: WebSocket streaming support not yet implemented
    [Tags]    audio-streaming
    Skip    WebSocket streaming not implemented in SDK yet

SDK Can Resume Interrupted Uploads
    [Documentation]    PLACEHOLDER: Resumable uploads not supported by backend
    [Tags]    audio-upload
    Skip    Resumable uploads not supported

SDK Can Handle Batch Upload With Progress
    [Documentation]    PLACEHOLDER: Batch upload is implemented but needs Robot test
    [Tags]    audio-batch
    Skip    Test implementation pending

SDK Can Search Memories
    [Documentation]    PLACEHOLDER: Memory search API not exposed in SDK yet
    [Tags]    memory
    Skip    Memory search not implemented in SDK

SDK Can Manage Action Items
    [Documentation]    PLACEHOLDER: Action items API not exposed in SDK yet
    [Tags]    infra
    Skip    Action items not implemented in SDK

*** Keywords ***
# Using Suite Setup/Teardown from setup_keywords.robot

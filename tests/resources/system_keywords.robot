*** Settings ***
Documentation    Health check, service readiness, and mock service management keywords
...
...              This file contains keywords for checking service health and managing mock services.
...              Keywords in this file handle API endpoint health checks, service status verification,
...              and starting/stopping mock services for testing.
...
...              Keywords in this file handle:
...              - Health endpoint checks
...              - Readiness endpoint checks
...              - Service availability verification
...              - Mock service lifecycle management
...
...              Keywords that should NOT be in this file:
...              - Docker service management (belong in setup_env_keywords.robot)
...              - Data management (belong in test_manager_keywords.robot)
...              - User/session management (belong in respective resource files)
Library          RequestsLibrary
Library          Process
Variables        ../setup/test_env.py


*** Keywords ***

Readiness Check
    [Documentation]    Verify that the readiness endpoint is accessible and returns 200
    [Tags]             health    api
    [Arguments]        ${base_url}=${API_URL}

    ${response}=    GET    ${base_url}/readiness    expected_status=200    timeout=2
    Should Be Equal As Integers    ${response.status_code}    200
    RETURN    ${True}

Health Check
    [Documentation]    Verify that the health endpoint is accessible and returns 200
    [Tags]             health    api
    [Arguments]        ${base_url}=${API_URL}

    ${response}=    GET    ${base_url}/health    expected_status=200    timeout=2
    Should Be Equal As Integers    ${response.status_code}    200
    RETURN    ${True}


Start Mock Transcription Server
    [Documentation]    Start the mock WebSocket transcription server on port 9999
    ...                Used for testing transcription workflows without external API dependencies.

    # Start mock server as background process
    ${handle}=    Start Process
    ...    python3    ${CURDIR}/../scripts/mock_transcription_server.py    --host    0.0.0.0    --port    9999
    ...    alias=mock_transcription_server
    ...    stdout=${OUTPUTDIR}/mock_transcription_server.log
    ...    stderr=STDOUT

    # Store process handle for cleanup
    Set Suite Variable    ${MOCK_TRANSCRIPTION_HANDLE}    ${handle}

    # Wait for server to start
    Sleep    2s

    Log    ✅ Started Mock Transcription Server on ws://localhost:9999


Stop Mock Transcription Server
    [Documentation]    Stop the mock WebSocket transcription server

    # Check if handle exists
    ${handle_exists}=    Run Keyword And Return Status    Variable Should Exist    ${MOCK_TRANSCRIPTION_HANDLE}

    IF    ${handle_exists}
        # Terminate the process gracefully
        Terminate Process    ${MOCK_TRANSCRIPTION_HANDLE}

        # Wait for process to exit
        ${result}=    Wait For Process    ${MOCK_TRANSCRIPTION_HANDLE}    timeout=5s    on_timeout=kill

        Log    ✅ Stopped Mock Transcription Server (exit code: ${result.rc})
    ELSE
        Log    ⚠️ Mock Transcription Server handle not found (may not have been started)
    END


Set Always Persist Enabled
    [Documentation]    Set the always_persist_enabled setting via API.
    ...                Requires admin session.
    [Arguments]    ${session}    ${enabled}=${True}

    ${settings}=    Create Dictionary    always_persist_enabled=${enabled}
    ${response}=    POST On Session    ${session}    /api/misc-settings    json=${settings}
    Should Be Equal As Integers    ${response.status_code}    200
    Log    ✅ Set always_persist_enabled=${enabled}


Get Always Persist Enabled
    [Documentation]    Get the current always_persist_enabled setting via API.
    ...                Requires admin session.
    [Arguments]    ${session}

    ${response}=    GET On Session    ${session}    /api/misc-settings
    Should Be Equal As Integers    ${response.status_code}    200
    ${settings}=    Set Variable    ${response.json()}
    ${enabled}=    Set Variable    ${settings}[always_persist_enabled]
    RETURN    ${enabled}

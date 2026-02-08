*** Settings ***
Documentation    ASR Service Keywords for transcription endpoint testing
...
...              This file contains keywords for testing ASR service endpoints.
...              Keywords in this file should handle ASR service interactions,
...              file uploads for transcription, and response validation helpers.
...
...              Examples of keywords that belong here:
...              - ASR health check and info retrieval
...              - Audio file upload for transcription
...              - ASR service URL management
...              - ASR service lifecycle (start/stop via docker compose)
...
...              Keywords that should NOT be in this file:
...              - Verification/assertion keywords (belong in tests)
...              - Backend API session management (use session_keywords.robot)
...              - Audio file operations (use audio_keywords.robot)
Library          RequestsLibrary
Library          Collections
Library          OperatingSystem
Library          Process
Variables        ../setup/test_env.py

*** Variables ***
${ASR_URL}           http://localhost:8765
${GPU_ASR_URL}       http://localhost:8767

*** Keywords ***

Upload Audio For ASR Transcription
    [Documentation]    Upload audio file to ASR /transcribe endpoint
    ...                Returns the full response object for verification in tests
    [Arguments]    ${audio_file}    ${base_url}=${ASR_URL}

    # Verify file exists
    File Should Exist    ${audio_file}

    # Create session if not exists
    ${session_exists}=    Run Keyword And Return Status
    ...    GET On Session    asr-session    /health    expected_status=any
    IF    not ${session_exists}
        Create Session    asr-session    ${base_url}    verify=True
    END

    # Upload file using multipart form
    ${file_data}=    Get Binary File    ${audio_file}
    &{files}=    Create Dictionary    file=${file_data}

    ${response}=    POST On Session    asr-session    /transcribe
    ...    files=${files}    expected_status=any

    RETURN    ${response}

Check ASR Service Health
    [Documentation]    Verify ASR service is healthy and responding
    [Arguments]    ${base_url}=${ASR_URL}

    Create Session    asr-health    ${base_url}    verify=True
    ${response}=    GET On Session    asr-health    /health    expected_status=200

    ${json}=    Set Variable    ${response.json()}
    Should Be Equal    ${json}[status]    healthy

Get ASR Service Info
    [Documentation]    Get ASR service info and return response JSON
    [Arguments]    ${base_url}=${ASR_URL}

    Create Session    asr-info    ${base_url}    verify=True
    ${response}=    GET On Session    asr-info    /info    expected_status=200

    RETURN    ${response.json()}

Wait For ASR Service Ready
    [Documentation]    Wait until ASR service is healthy (for GPU service startup)
    [Arguments]    ${base_url}=${GPU_ASR_URL}    ${timeout}=180s    ${interval}=10s

    Wait Until Keyword Succeeds    ${timeout}    ${interval}
    ...    Check ASR Service Health    ${base_url}

Start GPU ASR Service
    [Documentation]    Start a GPU ASR service via docker compose
    ...                Supports: transformers-asr, nemo-asr, faster-whisper-asr, parakeet-asr
    [Arguments]    ${service}=transformers-asr    ${model}=${EMPTY}    ${port}=8767

    ${asr_dir}=    Set Variable    ${CURDIR}/../../extras/asr-services

    # Build environment variables
    @{env_vars}=    Create List
    IF    "${model}" != "${EMPTY}"
        Append To List    ${env_vars}    ASR_MODEL=${model}
    END
    Append To List    ${env_vars}    ASR_PORT=${port}

    Log To Console    \n🚀 Starting ${service} on port ${port}...
    IF    "${model}" != "${EMPTY}"
        Log To Console    Model: ${model}
    END

    # Start the service
    ${result}=    Run Process    docker    compose    up    -d    --build    ${service}
    ...    cwd=${asr_dir}
    ...    env:ASR_MODEL=${model}
    ...    env:ASR_PORT=${port}

    IF    ${result.rc} != 0
        Log    STDOUT: ${result.stdout}
        Log    STDERR: ${result.stderr}
        Fail    Failed to start ${service}: ${result.stderr}
    END

    Log To Console    ✅ ${service} container started

Stop GPU ASR Service
    [Documentation]    Stop a GPU ASR service via docker compose
    [Arguments]    ${service}=transformers-asr

    ${asr_dir}=    Set Variable    ${CURDIR}/../../extras/asr-services

    Log To Console    \n🛑 Stopping ${service}...

    ${result}=    Run Process    docker    compose    stop    ${service}
    ...    cwd=${asr_dir}

    IF    ${result.rc} != 0
        Log    Warning: Failed to stop ${service}: ${result.stderr}    WARN
    ELSE
        Log To Console    ✅ ${service} stopped
    END

Remove GPU ASR Service
    [Documentation]    Stop and remove a GPU ASR service container
    [Arguments]    ${service}=transformers-asr

    ${asr_dir}=    Set Variable    ${CURDIR}/../../extras/asr-services

    Log To Console    \n🗑️ Removing ${service}...

    ${result}=    Run Process    docker    compose    rm    -f    -s    ${service}
    ...    cwd=${asr_dir}

    IF    ${result.rc} != 0
        Log    Warning: Failed to remove ${service}: ${result.stderr}    WARN
    ELSE
        Log To Console    ✅ ${service} removed
    END

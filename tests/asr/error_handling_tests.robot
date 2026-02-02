*** Settings ***
Documentation    ASR Service Error Handling Tests - validates error responses
...
...              These tests verify proper error handling in the ASR service:
...              - Missing file in transcribe request
...              - Invalid endpoints return 404
...              - Proper HTTP status codes for error conditions
...
...              Run with: make test-asr
Library          RequestsLibrary
Library          Collections
Resource         ../setup/setup_keywords.robot

Suite Setup      Suite Setup

*** Variables ***
${ASR_URL}    http://localhost:8765

*** Test Cases ***

ASR Transcribe Without File Returns 422
    [Documentation]    POST /transcribe without file should return 422 Unprocessable Entity
    [Tags]    infra

    Create Session    asr    ${ASR_URL}    verify=True
    ${response}=    POST On Session    asr    /transcribe    expected_status=422

    # 422 indicates validation error (missing required field)
    Should Be Equal As Integers    ${response.status_code}    422

ASR Invalid Endpoint Returns 404
    [Documentation]    Non-existent endpoint should return 404 Not Found
    [Tags]    infra

    Create Session    asr    ${ASR_URL}    verify=True
    ${response}=    GET On Session    asr    /invalid-endpoint    expected_status=404

    Should Be Equal As Integers    ${response.status_code}    404

ASR Health Endpoint Returns JSON Content Type
    [Documentation]    Health endpoint should return application/json content type
    [Tags]    health

    Create Session    asr    ${ASR_URL}    verify=True
    ${response}=    GET On Session    asr    /health    expected_status=200

    ${content_type}=    Get From Dictionary    ${response.headers}    Content-Type
    Should Contain    ${content_type}    application/json

ASR Info Endpoint Returns JSON Content Type
    [Documentation]    Info endpoint should return application/json content type
    [Tags]    infra

    Create Session    asr    ${ASR_URL}    verify=True
    ${response}=    GET On Session    asr    /info    expected_status=200

    ${content_type}=    Get From Dictionary    ${response.headers}    Content-Type
    Should Contain    ${content_type}    application/json

ASR Transcribe POST Only
    [Documentation]    GET request to /transcribe should return 405 Method Not Allowed
    [Tags]    infra

    Create Session    asr    ${ASR_URL}    verify=True
    ${response}=    GET On Session    asr    /transcribe    expected_status=405

    Should Be Equal As Integers    ${response.status_code}    405

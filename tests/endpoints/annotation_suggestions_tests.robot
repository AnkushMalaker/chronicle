*** Settings ***
Documentation    Annotation Suggestions Endpoint Tests
...
...              Tests for the GET /annotations/suggestions endpoint
...              used by the User Loop swipe review UI.
Library          RequestsLibrary
Library          Collections
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/user_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

*** Test Cases ***

Get Suggestions Returns Empty List When No Suggestions Exist
    [Documentation]    Verify suggestions endpoint returns empty list for a fresh user
    [Tags]    infra

    ${session}=    Get Admin API Session
    ${response}=    GET On Session    ${session}    /api/annotations/suggestions
    Should Be Equal As Integers    ${response.status_code}    200

    ${suggestions}=    Set Variable    ${response.json()}
    Should Be Equal As Integers    ${suggestions.__len__()}    0

Get Suggestions Requires Authentication
    [Documentation]    Verify suggestions endpoint returns 401 without auth token
    [Tags]    infra

    Get Anonymous Session    anon_session
    ${response}=    GET On Session    anon_session    /api/annotations/suggestions    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401

Get Suggestions Respects Limit Parameter
    [Documentation]    Verify suggestions endpoint accepts limit query parameter
    [Tags]    infra

    ${session}=    Get Admin API Session
    &{params}=    Create Dictionary    limit=5
    ${response}=    GET On Session    ${session}    /api/annotations/suggestions    params=${params}
    Should Be Equal As Integers    ${response.status_code}    200

    ${suggestions}=    Set Variable    ${response.json()}
    ${count}=    Get Length    ${suggestions}
    Should Be True    ${count} <= 5    Suggestions count should respect limit parameter

Non Admin User Can Access Own Suggestions
    [Documentation]    Verify non-admin users can access the suggestions endpoint
    [Tags]    infra	permissions

    # Create a regular test user
    ${session}=    Get Admin API Session
    ${user}=    Create Test User    ${session}    suggestions_test@example.com    testpass123

    # Login as the test user
    Create API Session    user_session    suggestions_test@example.com    testpass123

    ${response}=    GET On Session    user_session    /api/annotations/suggestions
    Should Be Equal As Integers    ${response.status_code}    200

    ${suggestions}=    Set Variable    ${response.json()}
    Should Be Equal As Integers    ${suggestions.__len__()}    0

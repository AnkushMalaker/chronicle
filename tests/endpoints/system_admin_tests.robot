*** Settings ***
Documentation    System and Admin API Tests
Library          RequestsLibrary
Library          Collections
Library          String
Library          OperatingSystem
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/user_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup
*** Test Cases ***

Get Diarization Settings Test
    [Documentation]    Test getting diarization settings (admin only)
    [Tags]    infra	permissions

    ${response}=       GET On Session    api    /api/diarization-settings
    Should Be Equal As Integers    ${response.status_code}    200

    ${settings}=       Set Variable    ${response.json()}
    Should Be True     isinstance($settings, dict)

Save Diarization Settings Test
    [Documentation]    Test saving diarization settings (admin only)
    [Tags]    infra	permissions

    # First get current settings
    ${get_response}=   GET On Session    api   /api/diarization-settings
    Should Be Equal As Integers    ${get_response.status_code}    200
    ${response_data}=    Set Variable    ${get_response.json()}
    ${current_settings}=    Set Variable    ${response_data}[settings]

    # Save the same settings (should succeed)
    ${response}=       POST On Session    api    /api/diarization-settings    json=${current_settings}
    Should Be Equal As Integers    ${response.status_code}    200

Get Speaker Configuration Test
    [Documentation]    Test getting user's speaker configuration
    [Tags]    infra	permissions

    ${response}=       GET On Session    api    /api/speaker-configuration
    Should Be Equal As Integers    ${response.status_code}    200

    ${config}=         Set Variable    ${response.json()}
    Should Be True     isinstance($config, dict)

    # Verify expected structure
    Dictionary Should Contain Key    ${config}    primary_speakers
    Dictionary Should Contain Key    ${config}    user_id
    Dictionary Should Contain Key    ${config}    status
    Should Be True     isinstance($config['primary_speakers'], list)

Update Speaker Configuration Test
    [Documentation]    Test updating user's speaker configuration
    [Tags]    infra	permissions

    # Update with empty speaker list
    ${speakers}=       Create List
    ${response}=       POST On Session    api    /api/speaker-configuration    json=${speakers}
    Should Be Equal As Integers    ${response.status_code}    200

Get Enrolled Speakers Test
    [Documentation]    Test getting enrolled speakers from speaker recognition service
    [Tags]    infra
     # Check if speaker service is actually available
    ${response}=       GET On Session    api   /api/speaker-service-status
    IF    ${response.json()}[service_available] == $False
        Skip    Speaker recognition service is not available or configured
    END

    ${response}=       GET On Session    api   /api/enrolled-speakers
    Should Be Equal As Integers    ${response.status_code}    200

    ${response_data}=  Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${response_data}    service_available
    Dictionary Should Contain Key    ${response_data}    speakers

    # If service is available, verify speakers data
    Should Be True    isinstance($response_data["speakers"], list)

Get Speaker Service Status Test
    [Documentation]    Test checking speaker recognition service status (admin only)
    [Tags]    infra	permissions

    ${response}=       GET On Session    api    /api/speaker-service-status
    Should Be Equal As Integers    ${response.status_code}    200

    ${status}=         Set Variable    ${response.json()}
    Should Be True     isinstance($status, dict)

    # Verify expected keys are present
    Dictionary Should Contain Key    ${status}    service_available
    Dictionary Should Contain Key    ${status}    healthy
    Dictionary Should Contain Key    ${status}    status

    # Check if speaker service is actually available
    IF    ${status}[service_available] == $False
        Skip    Speaker recognition service is not available or configured
    END

Get Memory Config Raw Test
    [Documentation]    Test getting raw memory configuration (admin only)
    [Tags]    infra	memory	permissions

    ${response}=       GET On Session    api    /api/admin/memory/config/raw
    Should Be Equal As Integers    ${response.status_code}    200

    # Raw config should be text/yaml
    ${config}=         Set Variable    ${response.text}
    Should Not Be Empty    ${config}

Validate Memory Config Test
    [Documentation]    Test validating memory configuration YAML (admin only)
    [Tags]    infra	memory	permissions

    # Test with valid YAML - use actual config from server
    ${get_response}=   GET On Session    api    /api/admin/memory/config/raw
    Should Be Equal As Integers    ${get_response.status_code}    200
    ${response_json}=  Set Variable    ${get_response.json()}
    ${valid_yaml}=     Set Variable    ${response_json}[config_yaml]
    &{data}=           Create Dictionary    config_yaml=${valid_yaml}
    ${response}=       POST On Session    api    /api/admin/memory/config/validate    json=${data}
    Should Be Equal As Integers    ${response.status_code}    200

    # Test with invalid YAML
    ${invalid_yaml}=   Set Variable    invalid: yaml: structure: [unclosed
    &{data}=           Create Dictionary    config_yaml=${invalid_yaml}
    ${response}=       POST On Session    api    /api/admin/memory/config/validate    json=${data}    expected_status=any
    Should Be True     ${response.status_code} in [400, 422]

Reload Memory Config Test
    [Documentation]    Test reloading memory configuration (admin only)
    [Tags]    infra	memory	permissions

    ${response}=       POST On Session    api    /api/admin/memory/config/reload
    Should Be Equal As Integers    ${response.status_code}    200

Delete All User Memories Test
    [Documentation]    Test deleting all memories for current user
    [Tags]    infra	memory	permissions

    ${response}=       DELETE On Session    api   /api/admin/memory/delete-all
    Should Be Equal As Integers    ${response.status_code}    200

    ${result}=         Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${result}    message


Non-Admin Cannot Access Admin Endpoints Test
    [Documentation]    Test that non-admin users cannot access admin endpoints
    [Tags]    infra	permissions

    # Create a non-admin user
    ${test_user}=      Create Test User    api
    Create API Session    user_session    email=${test_user}[email]    password=${TEST_USER_PASSWORD}

    # Test various admin endpoints
    ${endpoints}=      Create List
    ...                /api/diarization-settings
    ...                /api/speaker-service-status
    ...                /api/admin/memory/config/raw
    ...                /api/admin/memory/config/reload
    ...                /api/process-audio-files/jobs

    FOR    ${endpoint}    IN    @{endpoints}
      ${response}=   GET On Session    user_session    ${endpoint}    expected_status=any
      Should Be True    ${response.status_code} in [405, 403, 404]
    END

    # Cleanup
    Delete User    api    ${test_user}[id]

Unauthorized System Access Test
    [Documentation]    Test that system endpoints require authentication
    [Tags]    infra	permissions
    Get Anonymous Session    anon_session

    # Test endpoints that require authentication
    ${auth_endpoints}=    Create List
    ...                   /api/diarization-settings
    ...                   /api/speaker-configuration
    ...                   /api/enrolled-speakers
    ...                   /api/admin/memory/delete-all

    FOR    ${endpoint}    IN    @{auth_endpoints}
        ${response}=   GET On Session    anon_session    ${endpoint}    expected_status=any
        Should Be True    ${response.status_code} in [401, 405, 403]
    END

Invalid System Operations Test
    [Documentation]    Test invalid operations on system endpoints
    [Tags]    infra

    # Test saving invalid diarization settings
    ${invalid_settings}=    Create Dictionary    invalid_key=invalid_value
    ${response}=             POST On Session    api    /api/diarization-settings    json=${invalid_settings}    expected_status=any
    Should Be True          ${response.status_code} in [400, 422]

    # Test updating memory config with invalid YAML
    ${invalid_yaml}=    Set Variable    {invalid yaml content
    &{data}=            Create Dictionary    config_yaml=${invalid_yaml}
    ${response}=        POST On Session    api    /api/admin/memory/config/raw    json=${data}    expected_status=any
    Should Be True      ${response.status_code} in [400, 422]

Memory Configuration Workflow Test
    [Documentation]    Test complete memory configuration workflow (admin only)
    [Tags]    infra	memory	permissions

    # 1. Get current config
    ${get_response}=   GET On Session    api    /api/admin/memory/config/raw
    Should Be Equal As Integers    ${get_response.status_code}    200
    ${response_json}=  Set Variable    ${get_response.json()}
    ${original_config}=    Set Variable    ${response_json}[config_yaml]

    # 2. Validate the current config
    &{validate_data}=    Create Dictionary    config_yaml=${original_config}
    ${validate_response}=    POST On Session    api    /api/admin/memory/config/validate    json=${validate_data}
    Should Be Equal As Integers    ${validate_response.status_code}    200

    # 3. Reload config (should succeed)
    ${reload_response}=    POST On Session    api    /api/admin/memory/config/reload
    Should Be Equal As Integers    ${reload_response.status_code}    200

Speaker Configuration Workflow Test
    [Documentation]    Test complete speaker configuration workflow
    [Tags]    infra	permissions

    # 1. Get current speaker configuration
    ${get_response}=   GET On Session    api    /api/speaker-configuration
    Should Be Equal As Integers    ${get_response.status_code}    200
    ${current_config}=    Set Variable    ${get_response.json()}

    # 2. Update speaker configuration (with empty list)
    ${empty_speakers}=    Create List
    ${update_response}=   POST On Session    api    /api/speaker-configuration    json=${empty_speakers}
    Should Be Equal As Integers    ${update_response.status_code}    200

    # 3. Verify the update
    ${verify_response}=   GET On Session    api   /api/speaker-configuration
    Should Be Equal As Integers    ${verify_response.status_code}    200
    ${updated_config}=    Set Variable    ${verify_response.json()}

    # Verify response structure
    Dictionary Should Contain Key    ${updated_config}    primary_speakers
    Dictionary Should Contain Key    ${updated_config}    user_id
    Dictionary Should Contain Key    ${updated_config}    status

    # Should be empty list now
    ${speakers_list}=  Set Variable    ${updated_config}[primary_speakers]
    ${length}=         Get Length    ${speakers_list}
    Should Be Equal As Integers    ${length}    0


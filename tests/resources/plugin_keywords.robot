*** Settings ***
Documentation    Plugin testing resource file
...
...              This file contains keywords for plugin testing.
...              Keywords in this file should handle:
...              - Mock plugin creation and registration
...              - Plugin event subscription verification
...              - Event dispatch testing via API
...              - Wake word condition testing
...
Library          Collections
Library          OperatingSystem
Library          Process
Library          RequestsLibrary

*** Keywords ***
Create Mock Plugin Config
    [Documentation]    Create a mock plugin configuration for testing
    [Arguments]    ${events}    ${condition_type}=always    ${wake_words}=${NONE}

    ${config}=    Create Dictionary
    ...    enabled=True
    ...    events=${events}

    ${condition}=    Create Dictionary    type=${condition_type}
    IF    $wake_words is not None
        Set To Dictionary    ${condition}    wake_words=${wake_words}
    END
    Set To Dictionary    ${config}    condition=${condition}

    RETURN    ${config}

Verify Plugin Config Format
    [Documentation]    Verify plugin config follows new event-based format
    [Arguments]    ${config}

    Dictionary Should Contain Key    ${config}    events
    ...    msg=Plugin config should have 'events' field

    ${events}=    Get From Dictionary    ${config}    events
    Should Be True    isinstance(${events}, list)
    ...    msg=Subscriptions should be a list

    ${length}=    Get Length    ${events}
    Should Be True    ${length} > 0
    ...    msg=Plugin should subscribe to at least one event

Verify Event Name Format
    [Documentation]    Verify event name follows hierarchical naming convention
    [Arguments]    ${event}

    Should Contain    ${event}    .
    ...    msg=Event name should contain dot separator (e.g., 'transcript.streaming')

    ${parts}=    Split String    ${event}    .
    ${length}=    Get Length    ${parts}
    Should Be True    ${length} > 1
    ...    msg=Event should have domain and type (e.g., 'transcript.streaming')

Verify Event Matches Subscription
    [Documentation]    Verify an event would match a subscription
    [Arguments]    ${event}    ${subscription}

    Should Be Equal    ${event}    ${subscription}
    ...    msg=Event '${event}' should match subscription '${subscription}'

Get Test Plugins Config Path
    [Documentation]    Get path to test plugins configuration
    RETURN    ${CURDIR}/../config/plugins.test.yml

Verify HA Plugin Uses Events
    [Documentation]    Verify HomeAssistant plugin config uses event events

    ${plugins_yml}=    Get Test Plugins Config Path
    ${config_content}=    Get File    ${plugins_yml}

    Should Contain    ${config_content}    events:
    ...    msg=Plugin config should use 'events' field

    Should Contain    ${config_content}    transcript.streaming
    ...    msg=HA plugin should subscribe to 'transcript.streaming' event

    Should Not Contain    ${config_content}    access_level:
    ...    msg=Plugin config should NOT use old 'access_level' field

# Test Plugin Event Database Keywords

Clear Plugin Events
    [Documentation]    Clear all events from test plugin database via API
    ${response}=    DELETE On Session    api    /api/test/plugins/events
    Should Be Equal As Integers    ${response.status_code}    200
    Log    Cleared ${response.json()}[events_cleared] plugin events

Get Plugin Events By Type
    [Arguments]    ${event_type}
    [Documentation]    Query plugin events by event type via API
    ${response}=    GET On Session    api    /api/test/plugins/events    params=event_type=${event_type}
    Should Be Equal As Integers    ${response.status_code}    200
    RETURN    ${response.json()}[events]

Get Plugin Events By User
    [Arguments]    ${user_id}
    [Documentation]    Query plugin events by user_id
    # Note: Not implemented in API yet, keeping for backward compatibility
    ${response}=    GET On Session    api    /api/test/plugins/events
    Should Be Equal As Integers    ${response.status_code}    200
    ${all_events}=    Set Variable    ${response.json()}[events]
    # Filter by user_id in Robot Framework
    ${filtered}=    Create List
    FOR    ${event}    IN    @{all_events}
        IF    '${event}[user_id]' == '${user_id}'
            Append To List    ${filtered}    ${event}
        END
    END
    RETURN    ${filtered}

Get All Plugin Events
    [Documentation]    Get all events from test plugin database via API
    ${response}=    GET On Session    api    /api/test/plugins/events
    Should Be Equal As Integers    ${response.status_code}    200
    RETURN    ${response.json()}[events]

Get Plugin Event Count
    [Arguments]    ${event_type}=${NONE}
    [Documentation]    Get count of events via API, optionally filtered by type
    IF    '${event_type}' != 'None'
        ${response}=    GET On Session    api    /api/test/plugins/events/count    params=event_type=${event_type}
    ELSE
        ${response}=    GET On Session    api    /api/test/plugins/events/count
    END
    Should Be Equal As Integers    ${response.status_code}    200
    RETURN    ${response.json()}[count]

Verify Event Contains Data
    [Arguments]    ${event}    @{required_fields}
    [Documentation]    Verify event contains required data fields
    FOR    ${field}    IN    @{required_fields}
        Dictionary Should Contain Key    ${event}    ${field}
        ...    msg=Event should contain field '${field}'
    END

Wait For Plugin Event
    [Documentation]    Wait for at least one new plugin event of the specified type
    ...
    ...    Polls the database until the event count increases above the baseline.
    ...    Uses configurable timeout and retry interval for efficient polling.
    ...
    ...    Arguments:
    ...    - event_type: The event type to wait for (e.g., 'transcript.batch')
    ...    - baseline_count: The event count before the operation started
    ...    - timeout: Maximum time to wait (default: 30s)
    ...    - retry_interval: Time between polling attempts (default: 2s)
    ...    - conversation_id: Optional conversation_id to filter events (default: empty)
    [Arguments]    ${event_type}    ${baseline_count}    ${timeout}=30s    ${retry_interval}=2s    ${conversation_id}=${EMPTY}

    Wait Until Keyword Succeeds    ${timeout}    ${retry_interval}
    ...    Plugin Event Count Should Be Greater Than    ${event_type}    ${baseline_count}    ${conversation_id}

    # After successful wait, get the final count
    ${current_count}=    Get Plugin Event Count    ${event_type}
    ${new_events}=    Evaluate    ${current_count} - ${baseline_count}
    RETURN    ${new_events}

Plugin Event Count Should Be Greater Than
    [Documentation]    Assert that the current event count is greater than baseline
    ...
    ...    This keyword is used by Wait For Plugin Event for polling.
    ...    It will fail (causing a retry) until the condition is met.
    ...    Optionally filters by conversation_id if provided.
    [Arguments]    ${event_type}    ${baseline_count}    ${conversation_id}=${EMPTY}

    # Get all events of this type
    ${events}=    Get Plugin Events By Type    ${event_type}

    # If conversation_id filter specified, filter events
    IF    '${conversation_id}' != ''
        ${filtered_events}=    Create List
        FOR    ${event}    IN    @{events}
            ${event_data}=    Set Variable    ${event}[data]
            ${event_conv_id}=    Evaluate    $event_data.get('conversation_id', '')
            IF    '${event_conv_id}' == '${conversation_id}'
                Append To List    ${filtered_events}    ${event}
            END
        END
        ${current_count}=    Get Length    ${filtered_events}
    ELSE
        ${current_count}=    Get Length    ${events}
    END

    ${new_events}=    Evaluate    ${current_count} - ${baseline_count}

    # Build error message with conversation_id context if filtering
    IF    '${conversation_id}' != ''
        ${error_msg}=    Set Variable    Expected new ${event_type} events for conversation ${conversation_id}, but count is still ${current_count} (baseline: ${baseline_count})
    ELSE
        ${error_msg}=    Set Variable    Expected new ${event_type} events, but count is still ${current_count} (baseline: ${baseline_count})
    END

    Should Be True    ${new_events} > 0    msg=${error_msg}

    RETURN    ${new_events}

Should Contain Event
    [Documentation]    Verify event type exists, optionally filtered by conversation_id
    [Arguments]    ${event_type}    ${conversation_id}=${EMPTY}

    ${events}=    Get Plugin Events By Type    ${event_type}
    Should Not Be Empty    ${events}
    ...    msg=No events found for event type '${event_type}'

    IF    '${conversation_id}' != ''
        # Filter events by conversation_id in the data field
        ${found}=    Set Variable    ${False}
        FOR    ${event}    IN    @{events}
            ${event_data}=    Set Variable    ${event}[data]
            ${event_conv_id}=    Evaluate    $event_data.get('conversation_id', '')
            IF    '${event_conv_id}' == '${conversation_id}'
                ${found}=    Set Variable    ${True}
                BREAK
            END
        END
        Should Be True    ${found}
        ...    msg=No events found for conversation '${conversation_id}' with event type '${event_type}'
    END

Verify Event Metadata
    [Documentation]    Verify specific metadata field value exists in events
    [Arguments]    ${event_type}    ${metadata_key}    ${expected_value}    ${conversation_id}=${EMPTY}

    ${events}=    Get Plugin Events By Type    ${event_type}
    Should Not Be Empty    ${events}
    ...    msg=No events found for event type '${event_type}'

    # Collect conversation IDs for better error messages
    ${found_conv_ids}=    Create List
    ${found_metadata_values}=    Create List

    # Find matching event (optionally filtered by conversation_id)
    ${found}=    Set Variable    ${False}
    FOR    ${event}    IN    @{events}
        # Track conversation_id for debugging
        ${event_data}=    Set Variable    ${event}[data]
        ${event_conv_id}=    Evaluate    $event_data.get('conversation_id', '')
        IF    '${event_conv_id}' != ''
            Append To List    ${found_conv_ids}    ${event_conv_id}
        END

        # If conversation_id filter specified, check if this is the right conversation
        ${is_match}=    Set Variable    ${True}
        IF    '${conversation_id}' != ''
            IF    '${event_conv_id}' != '${conversation_id}'
                ${is_match}=    Set Variable    ${False}
            END
        END

        # If this is a matching event, check metadata
        IF    ${is_match}
            ${event_metadata}=    Set Variable    ${event}[metadata]
            Dictionary Should Contain Key    ${event_metadata}    ${metadata_key}
            ...    msg=Event metadata missing key '${metadata_key}'
            ${actual_value}=    Get From Dictionary    ${event_metadata}    ${metadata_key}
            Append To List    ${found_metadata_values}    ${actual_value}
            IF    '${actual_value}' == '${expected_value}'
                ${found}=    Set Variable    ${True}
                BREAK
            END
        END
    END

    # Build detailed error message if not found
    IF    not ${found}
        ${unique_conv_ids}=    Evaluate    list(set($found_conv_ids))
        IF    '${conversation_id}' != ''
            ${error_msg}=    Set Variable    No events found with metadata '${metadata_key}=${expected_value}' for conversation '${conversation_id}'. Found conversation IDs: ${unique_conv_ids}. Found metadata values: ${found_metadata_values}
        ELSE
            ${error_msg}=    Set Variable    No events found with metadata '${metadata_key}=${expected_value}' for event type '${event_type}'. Found conversation IDs: ${unique_conv_ids}. Found metadata values: ${found_metadata_values}
        END
        Fail    ${error_msg}
    END

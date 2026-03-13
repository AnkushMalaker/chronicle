*** Settings ***
Documentation    Debug Pipeline Step by Step
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup
*** Test Cases ***

Test server connection
    [Documentation]    Test connection to the server
    [Tags]    e2e

    Log    Testing server connection    INFO
    Skip    Test not written yet - placeholder test

Login to server
    [Documentation]    Test logging in to the server from mobile client
    [Tags]    e2e
    Log    Logging in to server    INFO
    Skip    Test not written yet - placeholder test

Scan bluetooth devices
    [Documentation]    Scan for available bluetooth devices
    [Tags]    e2e
    Log    Scanning bluetooth devices    INFO
    Skip    Test not written yet - placeholder test

Filter devices by omi
    [Documentation]    Filter scanned devices by omi
    [Tags]    e2e
    Log    Filtering devices by omi    INFO
    Skip    Test not written yet - placeholder test

Connect to bluetooth device
    [Documentation]    Connect to a bluetooth device
    [Tags]    e2e
    Log    Connecting to bluetooth device    INFO
    Skip    Test not written yet - placeholder test

Get device codec
    [Documentation]    Get the codec information from the device
    [Tags]    e2e
    Log    Getting device codec    INFO
    Skip    Test not written yet - placeholder test

Get device battery level
    [Documentation]    Get the battery level from the device
    [Tags]    e2e
    Log    Getting device battery level    INFO
    Skip    Test not written yet - placeholder test

Start audio stream
    [Documentation]    Start streaming audio from the device
    [Tags]    e2e
    Log    Starting audio stream    INFO
    Skip    Test not written yet - placeholder test

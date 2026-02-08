#!/bin/bash
# Run failed tests one by one

OUTPUTDIR="results/individual"
mkdir -p "$OUTPUTDIR"

# Define test names and their files
declare -A tests=(
    ["Audio Upload Job Tracking Test"]="endpoints/audio_upload_tests.robot"
    ["Audio Playback And Segment Timing Test"]="integration/integration_test.robot"
    ["WebSocket Disconnect Should Trigger Conversation Complete Event"]="integration/plugin_event_tests.robot"
    ["Conversation Job Created After Speech Detection"]="integration/websocket_streaming_tests.robot"
    ["Speech Detection Receives Transcription From Stream"]="integration/websocket_transcription_e2e_test.robot"
    ["Conversation Created With Valid Transcript"]="integration/websocket_transcription_e2e_test.robot"
)

echo "========================================"
echo "Running Failed Tests Individually"
echo "========================================"
echo ""

test_count=0
passed_count=0
failed_count=0

for test_name in "${!tests[@]}"; do
    test_count=$((test_count + 1))
    test_file="${tests[$test_name]}"
    safe_name=$(echo "$test_name" | sed 's/ /_/g')

    echo ""
    echo "========================================"
    echo "Test $test_count/6: $test_name"
    echo "File: $test_file"
    echo "========================================"
    echo ""

    # Run the test
    CREATE_FIXTURE=true uv run --with-requirements test-requirements.txt robot \
        --outputdir "$OUTPUTDIR/$safe_name" \
        --name "$test_name" \
        --console verbose \
        --loglevel INFO:INFO \
        --test "$test_name" \
        "$test_file"

    result=$?

    if [ $result -eq 0 ]; then
        echo ""
        echo "✓ PASSED: $test_name"
        passed_count=$((passed_count + 1))
    else
        echo ""
        echo "✗ FAILED: $test_name"
        failed_count=$((failed_count + 1))
    fi

    echo ""
    echo "----------------------------------------"
    sleep 2
done

echo ""
echo "========================================"
echo "Summary"
echo "========================================"
echo "Total tests: $test_count"
echo "Passed: $passed_count"
echo "Failed: $failed_count"
echo ""
echo "Individual test results saved in: $OUTPUTDIR/"

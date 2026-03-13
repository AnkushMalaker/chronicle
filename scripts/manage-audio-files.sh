#!/bin/bash

# Script to manage audio files in the advanced-backend container
# This script helps list, move, and organize audio files

set -e

NAMESPACE="chronicle"
POD_NAME=""
AUDIO_CHUNKS_DIR="/app/data/audio_chunks"
DATA_DIR="/app/data"
OLD_AUDIO_DIR="/app/audio_chunks"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to get the current pod name
get_pod_name() {
    POD_NAME=$(kubectl get pods -n $NAMESPACE -l app.kubernetes.io/name=advanced-backend -o jsonpath='{.items[0].metadata.name}')
    if [ -z "$POD_NAME" ]; then
        echo -e "${RED}Error: No advanced-backend pod found in namespace $NAMESPACE${NC}"
        exit 1
    fi
    echo -e "${BLUE}Using pod: $POD_NAME${NC}"
}

# Function to list audio files
list_audio_files() {
    echo -e "${GREEN}=== Listing Audio Files ===${NC}"

    echo -e "${YELLOW}Main data directory (/app/data):${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- ls -la $DATA_DIR/ 2>/dev/null || echo "Directory is empty or doesn't exist"

    echo -e "\n${YELLOW}Audio chunks directory (/app/data/audio_chunks):${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- ls -la $AUDIO_CHUNKS_DIR/ 2>/dev/null || echo "Directory is empty or doesn't exist"

    echo -e "\n${YELLOW}Old audio_chunks directory (if exists):${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- ls -la $OLD_AUDIO_DIR/ 2>/dev/null || echo "Old directory doesn't exist"

    echo -e "\n${YELLOW}All audio files in container (excluding test files):${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- find /app -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" | grep -v ".venv" | grep -v "node_modules" | head -20
}

# Function to move audio files from old location to new location
move_audio_files() {
    echo -e "${GREEN}=== Moving Audio Files ===${NC}"

    # Check if old directory exists and has files
    OLD_FILES=$(kubectl exec -n $NAMESPACE $POD_NAME -- find $OLD_AUDIO_DIR -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" 2>/dev/null | wc -l)

    if [ "$OLD_FILES" -gt 0 ]; then
        echo -e "${YELLOW}Found $OLD_FILES audio files in old location. Moving to new location...${NC}"

        # Create the new directory if it doesn't exist
        kubectl exec -n $NAMESPACE $POD_NAME -- mkdir -p $AUDIO_CHUNKS_DIR

        # Move all audio files
        kubectl exec -n $NAMESPACE $POD_NAME -- find $OLD_AUDIO_DIR -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" -exec mv {} $AUDIO_CHUNKS_DIR/ \;

        echo -e "${GREEN}Successfully moved audio files to $AUDIO_CHUNKS_DIR${NC}"
    else
        echo -e "${BLUE}No audio files found in old location${NC}"
    fi

    # Also check if there are audio files in the main data directory that should be in audio_chunks
    MAIN_DATA_FILES=$(kubectl exec -n $NAMESPACE $POD_NAME -- find $DATA_DIR -maxdepth 1 -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" 2>/dev/null | wc -l)

    if [ "$MAIN_DATA_FILES" -gt 0 ]; then
        echo -e "${YELLOW}Found $MAIN_DATA_FILES audio files in main data directory. Moving to audio_chunks subdirectory...${NC}"

        # Create the audio_chunks directory if it doesn't exist
        kubectl exec -n $NAMESPACE $POD_NAME -- mkdir -p $AUDIO_CHUNKS_DIR

        # Move all audio files from main data directory to audio_chunks
        kubectl exec -n $NAMESPACE $POD_NAME -- find $DATA_DIR -maxdepth 1 -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" -exec mv {} $AUDIO_CHUNKS_DIR/ \;

        echo -e "${GREEN}Successfully moved audio files to $AUDIO_CHUNKS_DIR${NC}"
    else
        echo -e "${BLUE}No audio files found in main data directory${NC}"
    fi
}

# Function to organize audio files by date
organize_by_date() {
    echo -e "${GREEN}=== Organizing Audio Files by Date ===${NC}"

    # Create year/month subdirectories
    kubectl exec -n $NAMESPACE $POD_NAME -- bash -c "
        cd $AUDIO_CHUNKS_DIR
        for file in *.wav *.mp3 *.m4a *.flac 2>/dev/null; do
            if [ -f \"\$file\" ]; then
                # Extract timestamp from filename (assuming format: timestamp_clientid_uuid.wav)
                timestamp=\$(echo \"\$file\" | cut -d'_' -f1)
                if [ -n \"\$timestamp\" ] && [ \"\$timestamp\" -gt 0 ] 2>/dev/null; then
                    # Convert timestamp to date
                    date_str=\$(date -d @\$timestamp +%Y-%m 2>/dev/null || echo \"unknown\")
                    mkdir -p \"\$date_str\"
                    mv \"\$file\" \"\$date_str/\"
                    echo \"Moved \$file to \$date_str/\"
                fi
            fi
        done
    "
}

# Function to clean up old audio files
cleanup_old_files() {
    echo -e "${GREEN}=== Cleaning Up Old Audio Files ===${NC}"

    # Remove files older than 30 days
    kubectl exec -n $NAMESPACE $POD_NAME -- bash -c "
        cd $AUDIO_CHUNKS_DIR
        find . -name '*.wav' -o -name '*.mp3' -o -name '*.m4a' -o -name '*.flac' | while read file; do
            if [ -f \"\$file\" ]; then
                # Check if file is older than 30 days
                if [ \$(find \"\$file\" -mtime +30) ]; then
                    echo \"Removing old file: \$file\"
                    rm \"\$file\"
                fi
            fi
        done
    "
}

# Function to show disk usage
show_disk_usage() {
    echo -e "${GREEN}=== Disk Usage ===${NC}"

    echo -e "${YELLOW}Audio chunks directory size:${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- du -sh $AUDIO_CHUNKS_DIR 2>/dev/null || echo "Directory doesn't exist"

    echo -e "\n${YELLOW}Total disk usage in /app/data:${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- du -sh /app/data

    echo -e "\n${YELLOW}Available disk space:${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- df -h /app/data
}

# Function to test audio endpoint
test_audio_endpoint() {
    echo -e "${GREEN}=== Testing Audio Endpoint ===${NC}"

    # Get a sample audio file from either location
    SAMPLE_FILE=$(kubectl exec -n $NAMESPACE $POD_NAME -- find $DATA_DIR -name "*.wav" | head -1)

    if [ -n "$SAMPLE_FILE" ]; then
        FILENAME=$(basename "$SAMPLE_FILE")
        echo -e "${YELLOW}Testing audio endpoint with file: $FILENAME${NC}"
        echo -e "${YELLOW}File location: $SAMPLE_FILE${NC}"

        # Test the audio endpoint
        RESPONSE=$(kubectl exec -n $NAMESPACE $POD_NAME -- curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000/audio/$FILENAME")

        if [ "$RESPONSE" = "200" ]; then
            echo -e "${GREEN}✅ Audio endpoint is working correctly${NC}"
        else
            echo -e "${RED}❌ Audio endpoint returned HTTP $RESPONSE${NC}"
        fi
    else
        echo -e "${BLUE}No audio files found to test with${NC}"
    fi
}

# Function to delete all audio files
delete_all_audio() {
    echo -e "${RED}=== DELETE ALL AUDIO FILES ===${NC}"
    echo -e "${YELLOW}⚠️  WARNING: This will permanently delete ALL audio files!${NC}"
    echo -e "${YELLOW}This action cannot be undone.${NC}"
    echo

    # Count total audio files first
    TOTAL_FILES=$(kubectl exec -n $NAMESPACE $POD_NAME -- find $DATA_DIR -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" 2>/dev/null | wc -l)

    if [ "$TOTAL_FILES" -eq 0 ]; then
        echo -e "${BLUE}No audio files found to delete.${NC}"
        return 0
    fi

    echo -e "${YELLOW}Found $TOTAL_FILES audio files to delete.${NC}"
    echo

    # Show some examples of files that will be deleted
    echo -e "${YELLOW}Example files that will be deleted:${NC}"
    kubectl exec -n $NAMESPACE $POD_NAME -- find $DATA_DIR -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" 2>/dev/null | head -5
    if [ "$TOTAL_FILES" -gt 5 ]; then
        echo -e "${YELLOW}... and $((TOTAL_FILES - 5)) more files${NC}"
    fi
    echo

    # Confirmation
    read -p "Are you ABSOLUTELY sure you want to delete ALL audio files? Type 'DELETE_ALL': " confirm
    echo

    if [ "$confirm" != "DELETE_ALL" ]; then
        echo -e "${BLUE}Operation cancelled. No files were deleted.${NC}"
        return 0
    fi

    echo -e "${RED}Deleting all audio files...${NC}"

    # Delete all audio files
    DELETED_COUNT=0
    kubectl exec -n $NAMESPACE $POD_NAME -- bash -c "
        cd $DATA_DIR
        find . -name '*.wav' -o -name '*.mp3' -o -name '*.m4a' -o -name '*.flac' | while read file; do
            if [ -f \"\$file\" ]; then
                echo \"Deleting: \$file\"
                rm \"\$file\"
                ((DELETED_COUNT++))
            fi
        done
        echo \"Deleted \$DELETED_COUNT files\"
    "

    # Verify deletion
    REMAINING_FILES=$(kubectl exec -n $NAMESPACE $POD_NAME -- find $DATA_DIR -name "*.wav" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.flac" 2>/dev/null | wc -l)

    if [ "$REMAINING_FILES" -eq 0 ]; then
        echo -e "${GREEN}✅ Successfully deleted all audio files!${NC}"
    else
        echo -e "${YELLOW}⚠️  Warning: $REMAINING_FILES files still remain${NC}"
    fi

    # Show disk usage after deletion
    echo -e "\n${YELLOW}Disk usage after deletion:${NC}"
    show_disk_usage
}

# Main menu
show_menu() {
    echo -e "${BLUE}=== Audio File Management Script ===${NC}"
    echo "1. List audio files"
    echo "2. Move audio files from old location"
    echo "3. Organize audio files by date"
    echo "4. Clean up old audio files (30+ days)"
    echo "5. Show disk usage"
    echo "6. Test audio endpoint"
    echo "7. Run all operations"
    echo "8. Delete ALL audio files (DANGEROUS)"
    echo "9. Exit"
    echo
    read -p "Select an option (1-9): " choice
}

# Main execution
main() {
    get_pod_name

    if [ $# -eq 0 ]; then
        # Interactive mode
        while true; do
            show_menu
            case $choice in
                1) list_audio_files ;;
                2) move_audio_files ;;
                3) organize_by_date ;;
                4) cleanup_old_files ;;
                5) show_disk_usage ;;
                6) test_audio_endpoint ;;
                7)
                    list_audio_files
                    move_audio_files
                    organize_by_date
                    show_disk_usage
                    test_audio_endpoint
                    ;;
                8) delete_all_audio ;;
                9) echo "Goodbye!"; exit 0 ;;
                *) echo -e "${RED}Invalid option${NC}" ;;
            esac
            echo
        done
    else
        # Command line mode
        case $1 in
            "list") list_audio_files ;;
            "move") move_audio_files ;;
            "organize") organize_by_date ;;
            "cleanup") cleanup_old_files ;;
            "usage") show_disk_usage ;;
            "test") test_audio_endpoint ;;
            "delete") delete_all_audio ;;
            "all")
                list_audio_files
                move_audio_files
                organize_by_date
                show_disk_usage
                test_audio_endpoint
                ;;
            *) echo "Usage: $0 [list|move|organize|cleanup|usage|test|delete|all]" ;;
        esac
    fi
}

main "$@"

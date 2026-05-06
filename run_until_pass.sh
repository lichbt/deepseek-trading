#!/bin/bash
echo "Starting infinite auto-research loop..."
echo "Will stop when a strategy passes (status='passed' in pipeline.db)"

# Loop indefinitely
while true; do
    echo "--- Starting new batch $(date) ---"
    python3 auto_research.py
    
    # Check if there's any passed strategy in the database
    PASSED_COUNT=$(sqlite3 pipeline.db "SELECT count(*) FROM strategies WHERE status='passed';")
    
    if [ "$PASSED_COUNT" -gt 0 ]; then
        echo "SUCCESS: Found $PASSED_COUNT passed strategies! Stopping loop."
        break
    fi
    
    echo "No passed strategies yet. Restarting loop in 10 seconds..."
    sleep 10
done

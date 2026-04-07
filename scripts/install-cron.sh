#!/bin/bash
# Install daily maintenance cron (macOS launchd or Linux crontab)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CRON_SCRIPT="$SCRIPT_DIR/../cron/daily-maintenance.sh"

if [[ "$(uname)" == "Darwin" ]]; then
    # macOS: create launchd plist
    PLIST="$HOME/Library/LaunchAgents/com.llm-wiki-stack.daily.plist"
    cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.llm-wiki-stack.daily</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$CRON_SCRIPT</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>4</integer>
        <key>Minute</key>
        <integer>7</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/llm-wiki-stack-cron.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/llm-wiki-stack-cron.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Installed launchd job: daily at 04:07"
    echo "Plist: $PLIST"
else
    # Linux: add crontab entry
    (crontab -l 2>/dev/null; echo "7 4 * * * $CRON_SCRIPT >> /tmp/llm-wiki-stack-cron.log 2>&1") | sort -u | crontab -
    echo "Installed crontab entry: daily at 04:07"
fi

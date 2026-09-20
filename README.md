

## Do Not Forget
 - Clean up `activity_notification_logs` table periodically
 - 

## Environment
PLAINWATCH_DB_PATH=plainwatch.db  <- default

PLAINAPP_URL=http://xxxx.xxx.xxx.xx:xxxx/
CLIENT_ID=<your-client-id without quotes>
BEARER_TOKEN=<your-bearer-toj=ken without quotes>

PLAINWATCH_TOPIC_INTERVAL_SECONDS=3600
MAX_CONCURRENT_ACTIVITIES=4
EXECUTOR_INTERVAL_SECONDS=3

PLAINWATCH_DB_PATH=plainwatch.db

OPENROUTER_API_KEY<openrouter API key>
OPENROUTER_MODEL=openai/gpt-5.6-luna

TIMELINE_MARKER=## Timeline
KEYWORDS_MARKER=## Keywords
TIMELINE_BLOCK_START=<!-- PLAINWATCH:TIMELINE_BLOCK -->
TIMELINE_BLOCK_END=<!-- /PLAINWATCH:TIMELINE_BLOCK -->
KEYWORD_ACTION_COOLDOWN=120
TIMELINE_ACTION_COOLDOWN=30
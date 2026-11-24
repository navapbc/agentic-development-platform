"""The Slack posting service, the only component that holds the shared Slack app's bot token."""

PORT = 8160
MESSAGES_PATH = "/v1/messages"
DEADLINE_HEADER = "X-Alert-Deadline-Seconds"
MIN_DEADLINE_SECONDS = 1.0
TOKEN_AUDIENCE = "slack-posting-service"

"""The Slack posting service, the only component that holds the shared Slack app's bot token."""

PORT = 8160
MESSAGES_PATH = "/v1/messages"
UPDATE_PATH = "/v1/messages/update"
ADMIN_AUDIENCE = "admin"
OWNER_PROOF_FIELD = "owner_token"
GROUPS_PATH = "/v1/approver-groups/"
DEADLINE_HEADER = "X-Alert-Deadline-Seconds"
MIN_DEADLINE_SECONDS = 1.0
SERVICE_ACCOUNT_AUDIENCE = "slack-posting-service"

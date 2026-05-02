"""Set required env vars before any flywheel/digest module is imported."""
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("AIRTABLE_PAT", "test-pat")
os.environ.setdefault("SLACK_WEBHOOK_URL", "https://example.com/webhook")

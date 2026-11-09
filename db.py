from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from .config import settings

client = AsyncIOMotorClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
db = client[settings.mongodb_db]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def audit(actor: str, event: str, detail: dict | str | None = None) -> None:
    """Append-only audit log: every agent decision, human approval and PayPal call."""
    await db.audit.insert_one({"ts": now(), "actor": actor, "event": event, "detail": detail})

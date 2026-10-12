"""Normalize the one cleanup configuration for execution and client display."""


def session_cleanup_policy(config: dict) -> dict:
    return {
        "enabled": bool(config.get("enabled", True)),
        "retention_days": float(config.get("retention_days", 30)),
        "empty_ttl_hours": float(config.get("empty_ttl_hours", 24)),
        "scan_interval_seconds": int(config.get("scan_interval_seconds", 3600)),
    }

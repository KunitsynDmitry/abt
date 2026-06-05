"""Local tools for the local_only example — no external APIs needed."""


def get_info() -> dict:
    """Return a fixed info object for demonstration purposes."""
    return {
        "user_name": "ABT Developer",
        "version": "1.0.0",
        "status": "ready",
    }

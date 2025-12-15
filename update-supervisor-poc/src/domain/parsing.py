import json
from .models import UpdateTarget


def _shorten_target_id(tid: str, max_len: int = 48) -> str:
    """
    If target_id is too long (common for OS updates), shorten it for display.
    """
    if len(tid) <= max_len:
        return tid
    return tid[:max_len - 3] + "..."


def parse_consent_required(raw_json: str):
    """
    Parse Aktualizr ConsentRequired Targets JSON.

    Works for:
    - Application updates (canonical docker-compose)
    - OS updates (OSTree targets)

    Returns a list of UpdateTarget items.
    """
    data = json.loads(raw_json)
    targets = data.get("targets", {})
    items = []

    for target_id, t in targets.items():
        custom = t.get("custom", {}) or {}

        # Prefer custom.name. If missing, use shortened target_id (OS ids are huge).
        name = custom.get("name")
        if not name:
            name = _shorten_target_id(target_id)

        version = str(custom.get("version", ""))

        # App updates usually provide tdx-description; OS usually doesn't.
        description = (
            custom.get("tdx-description")
            or custom.get("description")
            or ""
        )

        length = t.get("length", 0) or 0
        uri = custom.get("uri", "")

        # Detect update kind:
        # canonical_compose_file => application update
        # otherwise => likely OS/OSTree update
        is_app = bool(custom.get("canonical_compose_file"))
        kind = "application" if is_app else "os"

        items.append(UpdateTarget(
            target_id=target_id,
            name=name,
            version=version,
            description=description,
            length=length,
            uri=uri,
            kind=kind,  # <-- add this field in your model
        ))

    return items

import asyncio
from dbus_next.aio import MessageBus
from dbus_next import BusType, Variant
from dbus_next.errors import DBusError

# D-Bus service, object path, and interface exposed by Aktualizr
SERVICE = "org.uptane.Aktualizr"
PATH = "/org/uptane/aktualizr"
IFACE = "org.uptane.Aktualizr"


class AktualizrClient:
    """
    Thin asyncio-based client for Aktualizr's D-Bus API.

    This class mirrors the functionality we tested in aktualizr_lab.py,
    but as a reusable client that can be consumed by DBusWorker (Qt thread).

    Scope:
    - Read InstallUpdatesAutomatically (mode)
    - Read ConsentRequired (pending updates list in JSON)
    - Set InstallUpdatesAutomatically
    - Call methods: CheckForUpdates, Consent, Cancel

    Important:
    - This is NOT a full library for Aktualizr. It only wraps the supported D-Bus API.
    - All methods are async and must run inside an asyncio loop.
    """

    def __init__(self):
        # Asyncio MessageBus connection to the SYSTEM bus
        self.bus = None

        # org.freedesktop.DBus.Properties interface for reading/writing properties
        self.props = None

        # org.uptane.Aktualizr interface for calling methods
        self.aktualizr = None

        # Callback to notify higher layers (DBusWorker) when ConsentRequired changes.
        # Signature: fn(raw_json: str | None)
        self.on_consent_required_changed = None

    async def connect(self):
        """
        Connect to the SYSTEM D-Bus and bind to Aktualizr.

        Steps:
        1) Connect to system bus.
        2) Introspect Aktualizr service and path.
        3) Get proxy object and required interfaces.
        4) Subscribe to property change signals.
        """
        # Connect to the system bus
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        # Fetch introspection data (methods, properties, signals)
        intro = await self.bus.introspect(SERVICE, PATH)

        # Get proxy object for this service/path
        obj = self.bus.get_proxy_object(SERVICE, PATH, intro)

        # Standard Properties interface for Get/Set and change notifications
        self.props = obj.get_interface("org.freedesktop.DBus.Properties")

        # Aktualizr interface with supported methods (Consent, CheckForUpdates, Cancel, etc.)
        self.aktualizr = obj.get_interface(IFACE)

        # Subscribe to property changes (PropertiesChanged signal)
        self.props.on_properties_changed(self._on_props_changed)

    def _on_props_changed(self, iface, changed, invalidated):
        """
        Internal handler for PropertiesChanged signals.

        We only care about ConsentRequired. When it changes:
        - If non-empty -> an update requires consent
        - If empty     -> consent was consumed/cleared

        We forward this through the callback set by DBusWorker.
        """
        if "ConsentRequired" in changed:
            raw = changed["ConsentRequired"].value  # JSON string or empty

            if self.on_consent_required_changed:
                # Normalize empty string to None for consumers
                self.on_consent_required_changed(raw if raw else None)

    async def get_status(self):
        """
        Read initial/current status from Aktualizr.

        Returns:
        (mode, consent_required_json)
        - mode: InstallUpdatesAutomatically (0 or 1)
        - consent_required_json: raw JSON string from ConsentRequired, or empty
        """
        install_auto = await self.props.call_get(IFACE, "InstallUpdatesAutomatically")
        consent_req = await self.props.call_get(IFACE, "ConsentRequired")
        return install_auto.value, consent_req.value

    async def set_mode(self, value: int):
        """
        Set InstallUpdatesAutomatically property.

        value:
        - 0 -> automatic updates
        - 1 -> require user consent before installing updates
        """
        await self.props.call_set(
            IFACE,
            "InstallUpdatesAutomatically",
            Variant("i", value)
        )

    async def check_for_updates(self):
        """
        Call CheckForUpdates() method.

        This forces an immediate online update check.
        If Aktualizr is not idle, it silently ignores the request.

        After calling it, we re-read ConsentRequired and, if there is a pending
        consent request, we invoke the callback so the UI can react even if the
        property value was already pending and no PropertiesChanged signal is emitted.
        """
        await self.aktualizr.call_check_for_updates()

        # If a consent-change callback is registered, check the current status
        if self.on_consent_required_changed:
            try:
                _, consent_req = await self.get_status()
            except DBusError:
                # On transient D-Bus error, just skip notification
                return

            # Only notify when there is actually a pending consent request
            if consent_req:
                self.on_consent_required_changed(consent_req)

    async def consent(self, granted: bool, reason: str):
        """
        Call Consent(granted, reason) method.

        granted:
        - True  -> proceed with installation
        - False -> refuse update

        reason:
        - human-readable reason that gets reported upstream
        """
        await self.aktualizr.call_consent(granted, reason)

    async def cancel(self):
        """
        Call Cancel() method.

        Cancels an ongoing update asynchronously.
        Aktualizr will stop at a safe cancellation point.
        """
        await self.aktualizr.call_cancel()
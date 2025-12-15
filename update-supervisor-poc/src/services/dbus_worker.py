import asyncio
import os
import re
from PySide6.QtCore import QThread, Signal, Slot

from .aktualizr_client import AktualizrClient


# Default network interface used for activity estimation (rx+tx).
# This can be overridden at runtime:
#   OTA_NET_IFACE=eth0  (example)
NETWORK_INTERFACE = os.environ.get("OTA_NET_IFACE", "ethernet0")

# Log file produced on the host and mounted into the container.
# The worker "tails" this file to derive download progress and phase events.
# This can be overridden at runtime:
#   OTA_LOG_FILE=/path/to/aktualizr.log
DEFAULT_LOG_FILE = os.environ.get("OTA_LOG_FILE", "/home/torizon/ota-progress/aktualizr.log")

# Absolute path is used to avoid PATH issues when launching from a GUI context.
# This can be overridden at runtime if needed:
#   DBUS_SEND_ABS=/usr/local/bin/dbus-send
DBUS_SEND_ABS = os.environ.get("DBUS_SEND_ABS", "/usr/bin/dbus-send")

# Some GUI launchers provide a restricted environment; ensure a sane PATH.
# This is used only for subprocess calls (dbus-send), not for the Python runtime itself.
DEFAULT_PATH = os.environ.get(
    "DEFAULT_PATH",
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
)


class DBusWorker(QThread):
    """
    Background worker responsible for:
    - Interacting with Aktualizr through D-Bus (via AktualizrClient).
    - Emitting status changes (mode, consent required/cleared).
    - Reading update progress and lifecycle events from a host-provided log file.
    - Providing a lightweight network activity estimate (KB/s).
    - Requesting a system reboot via systemd-logind (system D-Bus).

    Implementation notes:
    - Runs an asyncio event loop inside a QThread to keep the UI responsive.
    - Communicates with the UI exclusively through Qt signals.
    """

    # --- Aktualizr / status signals ---
    # consent_required: emitted when an update requires user confirmation (raw JSON payload).
    # consent_cleared: emitted when a previously pending consent request disappears.
    # status_ready: emitted once after initial connection, carries (mode, pending-consent-json-or-empty).
    # error: emitted on fatal background errors (D-Bus connection issues, missing log file, etc.).
    consent_required = Signal(str)
    consent_cleared = Signal()
    status_ready = Signal(int, str)
    error = Signal(str)

    # --- Progress / phases derived from logs ---
    # download_progress: raw percentage 0..100 extracted from log lines.
    # network_activity: rough KB/s computed from rx+tx byte counters.
    # phase_event: coarse phase indicators derived from key log lines.
    download_progress = Signal(int)        # raw progress 0..100
    network_activity = Signal(float)       # KB/s
    phase_event = Signal(str)              # string identifier

    # --- Reboot lifecycle signals for the UI ---
    # reboot_started: emitted immediately when a reboot request is triggered.
    # reboot_failed: emitted if the reboot request returns an error before rebooting.
    reboot_started = Signal()
    reboot_failed = Signal(str)

    def __init__(self):
        super().__init__()

        # Event loop is created in run() (thread context).
        self.loop: asyncio.AbstractEventLoop | None = None

        # Thin client wrapper around Aktualizr's D-Bus interface.
        self.client = AktualizrClient()

        # File path for the mounted host log that will be tailed.
        self.log_file = DEFAULT_LOG_FILE

    def run(self):
        """
        QThread entry point. Creates and owns an asyncio event loop dedicated
        to D-Bus calls and background watchers.

        Why this design:
          - Qt GUI thread must remain responsive.
          - D-Bus calls and file tailing are naturally async tasks.
          - Signals are the safe way to send results back to the GUI thread.
        """
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def main():
            try:
                # Connect to Aktualizr and register callbacks that forward changes to Qt signals.
                await self.client.connect()
                self.client.on_consent_required_changed = self._emit_consent

                # Read initial status so the UI can reflect current mode and pending consent (if any).
                mode, consent = await self.client.get_status()
                self.status_ready.emit(mode, consent or "")

                # Start background tasks:
                # - _watch_log_file: parse download/install/reboot events from the mounted log
                # - _watch_network: compute simple throughput metric for display
                asyncio.create_task(self._watch_log_file())
                asyncio.create_task(self._watch_network())

                # Keep the worker alive indefinitely.
                # The QThread exits only if an exception bubbles out or the process terminates.
                await asyncio.Future()
            except Exception as e:
                # Any unhandled exception in the async loop is surfaced to the UI.
                self.error.emit(str(e))

        self.loop.run_until_complete(main())

    # -------------------------------------------------------------------------
    # Log watcher: derives progress and update phases by tailing a mounted log.
    # -------------------------------------------------------------------------
    async def _watch_log_file(self):
        """
        Tails a host-provided aktualizr log file (mounted inside the container) and emits:
        - download progress (raw percentage, when available)
        - lifecycle events (download_complete, install_started, need_reboot, ...)

        The parser is designed to handle repeated lines and minor message ordering
        differences across Aktualizr/OSTree versions.

        Important reliability note:
        - A pure "tail -n 0 -f" approach can miss events that were written before the UI started.
          To make demos deterministic, we first parse a small backlog of the most recent lines,
          then continue tailing new lines as they arrive.
        """
        # Download progress formats observed in aktualizr / ostree logs.
        # Some systems log "DownloadProgressReport", others log ostree-pull percentages.
        progress_re = re.compile(r"Event:\s*DownloadProgressReport,\s*Progress at\s*(\d+)%")
        ostree_receiving_objects_re = re.compile(r"ostree-pull:\s*Receiving objects:\s+(\d+)%")

        # Update lifecycle events (key markers in the log).
        install_started_re = re.compile(r"Event:\s*InstallStarted")
        install_complete_re = re.compile(r"Event:\s*(InstallTargetComplete|AllInstallsComplete).*Result")
        need_reboot_re = re.compile(r"Event:\s*AllInstallsComplete,\s*Result\s*-\s*NEED_COMPLETION")
        reboot_re = re.compile(r"About to reboot the system in order to apply pending updates")

        def is_download_complete_line(text: str) -> bool:
            # Robust substring matching: different aktualizr versions log completion differently.
            # Either line indicates the download phase is complete.
            return ("Event: DownloadTargetComplete" in text) or ("Event: AllDownloadsComplete" in text)

        def process_line(text: str) -> None:
            """
            Parses a single log line and emits the corresponding Qt signals.

            This is shared between:
              - Backlog processing (lines written before UI startup)
              - Live tailing (new lines appended after startup)

            Keeping a single parser prevents subtle differences between the two paths.
            """
            text = (text or "").strip()
            if not text:
                return

            # Download completion is a phase boundary; it matters even if no % > 50 was ever reported.
            if is_download_complete_line(text):
                self.phase_event.emit("download_complete")
                return

            # Progress emitted by ostree (often during object transfer).
            m_obj = ostree_receiving_objects_re.search(text)
            if m_obj:
                try:
                    p = int(m_obj.group(1))
                except ValueError:
                    p = 0
                self.download_progress.emit(max(0, min(100, p)))
                return

            # Progress emitted by aktualizr (explicit percentage events).
            m_prog = progress_re.search(text)
            if m_prog:
                try:
                    p = int(m_prog.group(1))
                except ValueError:
                    p = 0
                self.download_progress.emit(max(0, min(100, p)))
                return

            # Installation lifecycle markers:
            if install_started_re.search(text):
                self.phase_event.emit("install_started")
                return

            if need_reboot_re.search(text):
                # NEED_COMPLETION means install finished and a reboot is required to apply it.
                self.phase_event.emit("need_reboot")
                return

            if install_complete_re.search(text):
                self.phase_event.emit("install_complete")
                return

            if reboot_re.search(text):
                self.phase_event.emit("rebooting")
                return

        # Wait up to ~60 seconds for the log file to appear.
        # This covers cases where the host creates/mounts it after the container starts.
        for _ in range(60):
            if os.path.exists(self.log_file):
                break
            await asyncio.sleep(1.0)

        if not os.path.exists(self.log_file):
            # Missing log file prevents progress/phase UI updates; treat as a fatal worker error.
            self.error.emit(f"Log file not found: {self.log_file}")
            return

        # Backlog size: enough to capture recent phase boundaries without parsing the entire file.
        # This avoids missing key events if the update started before the UI was launched.
        BACKLOG_LINES = 300

        # Step 1: parse a small backlog (last N lines).
        # This makes behavior deterministic across reboots and startup order differences.
        try:
            with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            for line in lines[-BACKLOG_LINES:]:
                process_line(line)
        except Exception as e:
            # Not fatal: even if backlog read fails, live tailing may still work.
            self.error.emit(f"Failed to read log backlog: {e}")

        # Step 2: tail new lines as they arrive (like `tail -n 0 -f`).
        # We still seek to end here to avoid reprocessing the entire file in a loop.
        with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)

            while True:
                line = f.readline()
                if not line:
                    # No new data yet; sleep briefly and retry.
                    await asyncio.sleep(0.2)
                    continue

                process_line(line)

    # -------------------------------------------------------------------------
    # Network watcher: provides a rough KB/s estimate using rx+tx counters.
    # -------------------------------------------------------------------------
    async def _watch_network(self):
        """
        Emits a simple throughput estimate (KB/s) based on rx_bytes + tx_bytes.

        Notes:
          - This is a coarse metric intended only for user feedback.
          - If the interface counters are unavailable, network monitoring is disabled.
        """
        iface = NETWORK_INTERFACE
        rx_path = f"/sys/class/net/{iface}/statistics/rx_bytes"
        tx_path = f"/sys/class/net/{iface}/statistics/tx_bytes"

        def read_counter(path: str) -> int:
            # Reads a numeric counter from sysfs.
            # Any non-fatal parse errors return 0; missing files propagate as FileNotFoundError.
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return int(f.read().strip())
            except FileNotFoundError:
                raise
            except Exception:
                return 0

        try:
            prev_rx = read_counter(rx_path)
            prev_tx = read_counter(tx_path)
        except FileNotFoundError:
            # Interface counters not available in this environment.
            return

        prev_total = prev_rx + prev_tx

        while True:
            await asyncio.sleep(1.0)
            try:
                cur_rx = read_counter(rx_path)
                cur_tx = read_counter(tx_path)
            except FileNotFoundError:
                return

            cur_total = cur_rx + cur_tx
            delta_bytes = max(0, cur_total - prev_total)
            prev_total = cur_total

            kb_per_sec = float(delta_bytes) / 1024.0
            self.network_activity.emit(kb_per_sec)

    def _emit_consent(self, raw: str | None):
        """
        Callback used by AktualizrClient when the consent-required state changes.

        raw:
          - JSON payload string when consent is required
          - None / empty when consent is cleared
        """
        if raw:
            self.consent_required.emit(raw)
        else:
            self.consent_cleared.emit()

    # -------------------------------------------------------------------------
    # Public slots called by the UI thread
    # -------------------------------------------------------------------------
    @Slot(int)
    def set_mode(self, value: int):
        """
        Sets aktualizr mode (e.g., automatic vs consent-required).

        This schedules the actual D-Bus call on the worker's asyncio loop so the UI thread
        is never blocked.
        """
        asyncio.run_coroutine_threadsafe(self.client.set_mode(value), self.loop)

    @Slot()
    def check_for_updates(self):
        """
        Triggers an update check against the remote server.

        The result will appear asynchronously via:
          - consent_required (if a user decision is needed), or
          - timeout / no-state-change at the UI level if nothing is found.
        """
        asyncio.run_coroutine_threadsafe(self.client.check_for_updates(), self.loop)

    @Slot(bool, str)
    def send_consent(self, granted: bool, reason: str):
        """
        Sends user consent decision to aktualizr when required.

        granted:
          - True  -> accept the update
          - False -> refuse the update

        reason:
          - human-readable reason logged on the system side
        """
        asyncio.run_coroutine_threadsafe(self.client.consent(granted, reason), self.loop)

    @Slot()
    def cancel_update(self):
        """
        Requests cancellation of an ongoing update operation.
        Behavior depends on the current Aktualizr state and backend conditions.
        """
        asyncio.run_coroutine_threadsafe(self.client.cancel(), self.loop)

    # -------------------------------------------------------------------------
    # Reboot (systemd-logind via system D-Bus)
    # -------------------------------------------------------------------------
    @Slot()
    def reboot_now(self):
        """
        Requests a system reboot through systemd-logind via the system bus:

            org.freedesktop.login1.Manager.Reboot(boolean interactive=false)

        Requirements:
        - The system bus socket is available inside the container (e.g. mounted at
            /var/run/dbus/system_bus_socket).
        - The caller is authorized by the system policy (polkit) to request a reboot.
        """
        self.reboot_started.emit()
        asyncio.run_coroutine_threadsafe(self._reboot_via_dbus(), self.loop)

    def _format_proc_failure(self, cmd: list[str], rc: int, out: bytes, err: bytes) -> str:
        """
        Converts a subprocess failure into a readable message for the UI.

        This includes:
          - return code
          - command line
          - stdout/stderr if available
        """
        out_s = (out or b"").decode(errors="ignore").strip()
        err_s = (err or b"").decode(errors="ignore").strip()
        cmd_s = " ".join(cmd)

        msg = f"D-Bus reboot failed (rc={rc}): {cmd_s}"
        if out_s:
            msg += f"\n\nstdout:\n{out_s}"
        if err_s:
            msg += f"\n\nstderr:\n{err_s}"
        return msg

    async def _reboot_via_dbus(self):
        """
        Performs the reboot request using dbus-send. On success, the system restart may
        interrupt the normal response flow; this is expected behavior.
        """
        try:
            if not os.path.exists(DBUS_SEND_ABS):
                self.reboot_failed.emit(
                    f"Missing {DBUS_SEND_ABS}. Install dbus-send in the container (package: dbus)."
                )
                return

            # Ensure subprocess sees a valid PATH even when the app is started from a GUI launcher.
            env = os.environ.copy()
            env["PATH"] = DEFAULT_PATH

            cmd = [
                DBUS_SEND_ABS,
                "--system",
                "--print-reply",
                "--dest=org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager.Reboot",
                "boolean:false",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await proc.communicate()

            # If the call returns, validate the return code and surface details if it failed.
            if proc.returncode != 0:
                self.reboot_failed.emit(self._format_proc_failure(cmd, proc.returncode, out, err))

        except Exception as e:
            # Catch-all: if subprocess creation or execution fails, report to UI.
            self.reboot_failed.emit(f"D-Bus reboot failed: {e}")
from PySide6.QtCore import Qt, QSize, QRectF, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QBrush
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QPushButton,
    QLabel,
    QMessageBox,
    QAbstractButton,
    QProgressBar,
)

from services.dbus_worker import DBusWorker
from domain.parsing import parse_consent_required


class ToggleSwitch(QAbstractButton):
    """
    Simple iOS-like toggle switch.
    - unchecked: automatic updates
    - checked:   require user consent
    """
    toggled_value = Signal(bool)

    def __init__(self, parent=None, width=110, height=56):
        super().__init__(parent)

        # This widget behaves like a two-state button (on/off).
        self.setCheckable(True)

        # Fixed size to match the UI layout proportions.
        self._width = width
        self._height = height
        self.setMinimumSize(width, height)

        # UX: show a hand cursor to indicate it is clickable.
        self.setCursor(Qt.PointingHandCursor)

        # Emit a dedicated signal with the current boolean state.
        # This decouples UI painting from the business logic.
        self.clicked.connect(lambda: self.toggled_value.emit(self.isChecked()))

    def sizeHint(self):
        return QSize(self._width, self._height)

    def paintEvent(self, event):
        # Custom painting so the switch looks like a modern toggle component.
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Colors are defined here so the widget is self-contained.
        bg_off = QColor("#444444")
        bg_on = QColor("#2d89ef")
        knob_color = QColor("#ffffff")

        rect = QRectF(0, 0, self._width, self._height)
        radius = self._height / 2

        # Draw switch background.
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(bg_on if self.isChecked() else bg_off))
        p.drawRoundedRect(rect, radius, radius)

        # Draw the knob.
        knob_d = self._height - 8
        knob_y = 4
        knob_x = self._width - knob_d - 4 if self.isChecked() else 4

        p.setBrush(QBrush(knob_color))
        p.drawEllipse(QRectF(knob_x, knob_y, knob_d, knob_d))
        p.end()


"""
Update Supervisor UI (Aktualizr / Torizon)

Data sources:
  - D-Bus (via DBusWorker / AktualizrClient): update mode, consent, and commands.
  - Host log file (mounted into container): download/install phases and progress.

Progress bar model:
  - Stage A (0..50): time-based "preparing" (metadata scan). This avoids showing
    misleading high percentages before the real download begins.
  - Stage B (real % > 50):
      * If the first real value is high (e.g., 75), the bar jumps to that value.
      * Subsequent values are scaled to reach 94 max.
  - download_complete forces 95 even if no real progress > 50 was ever reported.

Installation:
  - install_started / install_complete update the UI text.
  - need_reboot triggers a confirmation popup and reboots via DBusWorker.

Design note:
  Log/event ordering may vary across devices/backends; the UI must not depend on a
  strict sequence to progress correctly.
"""


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        # Fixed resolution used by the reference HMI.
        # If the target display differs, this can be adapted to responsive layout.
        self.setFixedSize(1280, 800)
        self.setStyleSheet("background-color: #0b0f14; color: white;")

        # Main vertical layout. All elements are centered for kiosk-style UI.
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(30)

        # Main title.
        self.title = QLabel("Update Supervisor")
        self.title.setStyleSheet("font-size: 54px; font-weight: 700;")
        self.title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title)

        # High-level status text (phase / current action).
        self.info = QLabel("Connecting to Aktualizr...")
        self.info.setStyleSheet("font-size: 28px; color: #c9d1d9;")
        self.info.setAlignment(Qt.AlignCenter)
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        # Progress bar: hidden until an update flow starts.
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)  # UI uses text labels instead of inside-bar text.
        self.progress_bar.setFixedWidth(520)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #30363d;
                border-radius: 8px;
                background-color: #161b22;
            }
            QProgressBar::chunk {
                background-color: #2d89ef;
                border-radius: 8px;
            }
        """)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # Network throughput indicator, fed by DBusWorker network sampling.
        self.net_label = QLabel("")
        self.net_label.setStyleSheet("font-size: 18px; color: #8b949e;")
        self.net_label.setAlignment(Qt.AlignCenter)
        self.net_label.setMinimumWidth(520)
        self.net_label.setVisible(False)
        layout.addWidget(self.net_label)

        # Toggle label.
        self.mode_label = QLabel("Require user consent for updates")
        self.mode_label.setStyleSheet("font-size: 26px; color: #c9d1d9;")
        self.mode_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.mode_label)

        # Toggle switch for consent policy.
        self.switch_mode = ToggleSwitch(width=110, height=56)
        self.switch_mode.toggled_value.connect(self.on_mode_toggled)
        layout.addWidget(self.switch_mode, alignment=Qt.AlignCenter)

        # Manual check button (initiates server check and potentially triggers consent UI).
        self.btn_check = QPushButton("Check for updates")
        self.btn_check.setFixedSize(520, 140)
        self.btn_check.setStyleSheet("""
            QPushButton {
                font-size: 34px;
                font-weight: 600;
                background-color: #2d89ef;
                border-radius: 16px;
                padding: 12px;
            }
            QPushButton:hover { background-color: #1b5fbf; }
            QPushButton:pressed { background-color: #144a94; }
            QPushButton:disabled {
                background-color: #30363d;
                color: #8b949e;
            }
        """)
        self.btn_check.clicked.connect(self.on_check_clicked)
        layout.addWidget(self.btn_check)

        # Footer line: used for more verbose status or for errors that must be visible.
        self.footer = QLabel("")
        self.footer.setStyleSheet("font-size: 20px; color: #8b949e;")
        self.footer.setAlignment(Qt.AlignCenter)
        self.footer.setWordWrap(True)
        self.footer.setMinimumWidth(520)
        layout.addWidget(self.footer)

        # Tracks whether a "Check for updates" flow is currently active.
        # This affects how consent-required notifications are presented.
        self.manual_check_active = False

        # Update phases used by the UI
        # idle | preparing | downloading | downloaded | installing | need_reboot
        #
        # Important: log events may arrive out-of-order depending on backend behavior.
        # This is why the code avoids assuming a strict sequence.
        self.phase = "idle"
        self.phase_progress = 0.0

        # Download progress math:
        # - starting_pct is captured at the first real progress > 50
        # - last_raw_pct prevents the UI from moving backwards
        #
        # Some environments may report repeated progress values; those are accepted.
        self.starting_pct = None
        self.last_raw_pct = -1

        # Prevents the reboot popup from appearing multiple times.
        self.reboot_prompt_shown = False

        # Stage A timer: produces a smooth fake progress from 0..50 while metadata is processed
        #
        # The timer is intentionally independent from real download progress:
        # real download progress may never be reported > 50 on some systems.
        self.phase_timer = QTimer(self)
        self.phase_timer.setInterval(1000)  # 1 second tick (requested)
        self.phase_timer.timeout.connect(self.on_phase_tick)

        # Timeout for "Check for updates" to avoid a "waiting forever" state.
        self.check_timeout = QTimer(self)
        self.check_timeout.setSingleShot(True)
        self.check_timeout.timeout.connect(self.on_check_timeout)

        # Background worker thread:
        # - connects to Aktualizr via D-Bus
        # - tails a host log file for phase/progress events
        # - samples network activity
        self.worker = DBusWorker()
        self.worker.status_ready.connect(self.on_status_ready)
        self.worker.consent_required.connect(self.on_consent_required)
        self.worker.consent_cleared.connect(self.on_consent_cleared)
        self.worker.error.connect(self.on_error)
        self.worker.download_progress.connect(self.on_download_progress_raw)
        self.worker.phase_event.connect(self.on_phase_event)
        self.worker.network_activity.connect(self.on_network_activity)

        # Optional reboot lifecycle signals (if provided by the worker)
        # If the worker does not implement these signals, the hasattr check prevents runtime errors.
        if hasattr(self.worker, "reboot_failed"):
            self.worker.reboot_failed.connect(self.on_reboot_failed)
        if hasattr(self.worker, "reboot_started"):
            self.worker.reboot_started.connect(self.on_reboot_started)

        self.worker.start()

    # --------------------------
    # Small UI helpers
    # --------------------------
    def _is_update_flow_active(self) -> bool:
        # Used to lock/unlock UI actions while the update flow is active.
        return self.phase in ("preparing", "downloading", "downloaded", "installing", "need_reboot")

    def _lock_ui(self, locked: bool):
        # Disable interactions that could conflict with the current update state.
        self.btn_check.setEnabled(not locked)
        self.switch_mode.setEnabled(not locked)

    def _advance_to_target(self, target: int, step: float = 1.0):
        """Smoothly increments the progress bar up to a target value."""
        target = max(0, min(100, int(target)))
        if target > self.phase_progress:
            new_val = self.phase_progress + step
            if new_val > target:
                new_val = float(target)
            self.phase_progress = new_val
            self.progress_bar.setValue(int(self.phase_progress))

    def _set_progress_floor(self, value: int):
        """Guarantees the progress bar never moves backwards."""
        value = max(0, min(100, int(value)))
        if value > self.phase_progress:
            self.phase_progress = float(value)
            self.progress_bar.setValue(int(self.phase_progress))

    def _reset_progress_math(self):
        # Resets all state related to progress tracking.
        self.starting_pct = None
        self.last_raw_pct = -1

    def reset_progress_state(self):
        """Return the UI to the idle state."""
        self.phase = "idle"
        self.phase_progress = 0.0
        self.reboot_prompt_shown = False
        self._reset_progress_math()

        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.net_label.setText("")
        self.net_label.setVisible(False)
        self.phase_timer.stop()
        self._lock_ui(False)

    # --------------------------
    # Phase transitions
    # --------------------------
    def start_update_flow(self):
        """Starts the UI flow after the user accepts an update."""
        self.phase = "preparing"
        self.phase_progress = 0.0
        self.reboot_prompt_shown = False
        self._reset_progress_math()

        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self.net_label.setText("Measuring network activity...")
        self.net_label.setVisible(True)

        self.info.setText("Preparing update (checking metadata)...")
        self.footer.setText("Analyzing current system and update contents. This may take about a minute.")

        self._lock_ui(True)
        self.phase_timer.start()

    def switch_to_preparing(self):
        # This is typically entered after the user accepts consent and the system begins work.
        if self.phase == "preparing":
            return
        self.phase = "preparing"
        self.info.setText("Preparing update (checking metadata)...")
        self.footer.setText("Analyzing current system and update contents. This may take about a minute.")
        self.phase_timer.start()

    def switch_to_downloading(self):
        # Once real download progress is observed, Stage A should no longer drive the bar.
        if self.phase == "downloading":
            return
        self.phase_timer.stop()
        self.phase = "downloading"
        self.info.setText("Downloading update...")
        self.footer.setText("Downloading the update. This may take several minutes.")

    def switch_to_installing(self):
        # Installing is treated as "almost done": the progress is forced to 95.
        if self.phase == "installing":
            return
        self.phase_timer.stop()
        self.phase = "installing"
        self._set_progress_floor(95)
        self.info.setText("Applying update...")
        self.footer.setText("Finalizing changes. A reboot is required to complete the update.")

    # --------------------------
    # Stage A: fake progress 0..50
    # --------------------------
    def on_phase_tick(self):
        # Stage A is only active in the "preparing" phase.
        # It provides steady feedback while metadata is processed.
        if self.phase != "preparing":
            return
        if self.phase_progress < 50:
            self._advance_to_target(50, step=0.5)

    # --------------------------
    # Mode + Check flow
    # --------------------------
    def on_status_ready(self, mode: int, consent_raw: str):
        # Initial status comes from the worker once it connects to Aktualizr.
        self.switch_mode.blockSignals(True)
        self.switch_mode.setChecked(mode == 1)
        self.switch_mode.blockSignals(False)

        mode_txt = "Require consent" if mode == 1 else "Automatic"
        pending_txt = "Yes" if consent_raw else "No"
        self.info.setText(f"Mode: {mode_txt}")
        self.footer.setText(f"Pending consent request: {pending_txt}")

    def on_mode_toggled(self, checked: bool):
        # Changing the mode while the update is in progress is not supported.
        if self._is_update_flow_active() and self.phase != "idle":
            self.info.setText("An update is in progress. Mode cannot be changed now.")
            return

        value = 1 if checked else 0
        self.worker.set_mode(value)
        mode_txt = "Require consent" if checked else "Automatic"
        self.info.setText(f"Mode: {mode_txt}")

    def on_check_clicked(self):
        # Manual check is a user-driven action; if an update is already active, ignore.
        if self._is_update_flow_active() and self.phase != "idle":
            self.info.setText("An update is already in progress.")
            self.footer.setText("Please wait until the current update is finished.")
            return

        self.manual_check_active = True
        self.info.setText("Checking for updates on the server...")
        self.footer.setText("")
        self.worker.check_for_updates()
        self.check_timeout.start(15000)

    def on_check_timeout(self):
        # If the system did not transition into an update flow, treat it as "no updates".
        if self.phase == "idle":
            self.info.setText("No updates found.")
            self.footer.setText("Your system is up to date.")
            self.reset_progress_state()

        self.manual_check_active = False

    def on_consent_required(self, raw_json: str):
        # Stop timeout as soon as we get a response that implies there is an update.
        if self.check_timeout.isActive():
            self.check_timeout.stop()

        # If consent required arrives outside the manual check flow, present passive info only.
        if not self.manual_check_active:
            self.info.setText("An update is available (pending consent).")
            self.footer.setText("Press 'Check for updates' to review and apply it.")
            return

        # Parse consent payload and present a modal confirmation to the user.
        targets = parse_consent_required(raw_json)
        if not targets:
            self.manual_check_active = False
            return

        t = targets[0]
        msg = (
            f"An update is available.\n\n"
            f"Application: {t.name}\n"
            f"New version: {t.version}\n\n"
            f"{t.description or 'No description provided.'}"
        )

        box = QMessageBox(self)
        box.setWindowTitle("Update Available")
        box.setText(msg)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        box.setStyleSheet("QLabel { font-size: 22px; } QPushButton { font-size: 20px; }")

        ret = box.exec()
        if ret == QMessageBox.Yes:
            self.start_update_flow()
            self.worker.send_consent(True, "Accepted via UI")
        else:
            self.info.setText("Update refused.")
            self.footer.setText("")
            self.worker.send_consent(False, "Refused via UI")

        self.manual_check_active = False

    def on_consent_cleared(self):
        # Consent cleared means the request is no longer pending.
        # In the middle of download it is a normal state transition.
        if self.phase in ("preparing", "downloading"):
            self.info.setText("Consent sent. Waiting for download to complete...")
            self.footer.setText("Monitoring update progress...")

    # --------------------------
    # Stage B: real progress (may or may not appear)
    # --------------------------
    def on_download_progress_raw(self, raw_progress: int):
        """
        Handles download progress reported by logs.

        This UI supports both real-world behaviors:
          1) The system reports real percentage values > 50 during download.
          2) The system does not report progress > 50 and jumps directly to
             download_complete. In this case the bar stays at 50 until that event.

        If the first real value is, for example, 75%, the bar jumps to 75 immediately.
        From that point forward, remaining progress is scaled to reach 94 before
        download_complete forces 95.
        """
        # Progress events can arrive at any time except before the UI starts.
        if self.phase == "idle":
            return

        try:
            raw_progress = int(raw_progress)
        except Exception:
            return

        raw_progress = max(0, min(100, raw_progress))

        # Avoid moving backwards due to repeated/out-of-order log lines.
        # Equal values are allowed (some backends repeat the same progress).
        if raw_progress < self.last_raw_pct:
            return
        self.last_raw_pct = raw_progress

        # Stage A is timer-driven (0..50). Raw <= 50 is typically metadata-related.
        # Therefore, it does not move the bar.
        if raw_progress <= 50:
            return

        # When raw > 50 appears, the download is actually in progress.
        # At this point, Stage A is stopped and the UI enters "downloading".
        if self.phase != "downloading":
            self.switch_to_downloading()

        # First real point: jump directly to that percentage (capped to 94).
        # This is important when the first visible raw is already high (e.g., 75).
        if self.starting_pct is None:
            self.starting_pct = raw_progress
            self._set_progress_floor(min(raw_progress, 94))
            self.info.setText(f"Downloading update... {raw_progress}%")
            return

        # After the first real point, scale remaining progress into [start..94].
        start = self.starting_pct
        denom = max(1, (100 - start))
        scaled = start + ((raw_progress - start) / denom) * (94 - start)
        scaled_i = int(scaled)
        if scaled_i >= 95:
            scaled_i = 94

        self._set_progress_floor(scaled_i)
        self.info.setText(f"Downloading update... {raw_progress}%")

    # --------------------------
    # Reboot (requested via DBusWorker)
    # --------------------------
    def _show_reboot_prompt(self):
        # Reboot is initiated only after a NEED_COMPLETION state is observed.
        # This prompt is shown once; the guard prevents duplicate popups.
        if self.reboot_prompt_shown:
            return
        self.reboot_prompt_shown = True

        box = QMessageBox(self)
        box.setWindowTitle("Reboot Required")
        box.setText("The update has been applied and requires a reboot to complete.\n\nReboot now?")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        box.setStyleSheet("QLabel { font-size: 22px; } QPushButton { font-size: 20px; }")

        ret = box.exec()
        if ret != QMessageBox.Yes:
            self.info.setText("Reboot postponed.")
            self.footer.setText("You can reboot later to complete the update.")
            return

        # The reboot request itself is handled by DBusWorker (system D-Bus / logind).
        # If reboot succeeds, the system may restart before the UI receives any further events.
        try:
            self.worker.reboot_now()
            self.info.setText("Rebooting to complete the update...")
            self.footer.setText("Please wait while the device restarts.")
        except Exception as e:
            self.info.setText("Reboot failed.")
            self.footer.setText(str(e))

            err_box = QMessageBox(self)
            err_box.setWindowTitle("Reboot failed")
            err_box.setText(str(e))
            err_box.setStandardButtons(QMessageBox.Ok)
            err_box.setStyleSheet("QLabel { font-size: 18px; } QPushButton { font-size: 18px; }")
            err_box.exec()

    def on_reboot_started(self):
        # Optional: used if the worker emits a lifecycle signal for reboot initiation.
        self.info.setText("Rebooting to complete the update...")
        self.footer.setText("Please wait while the device restarts.")

    def on_reboot_failed(self, msg: str):
        # Optional: used if the worker emits a lifecycle signal for reboot failures.
        self.info.setText("Reboot failed.")
        self.footer.setText(msg)

        err_box = QMessageBox(self)
        err_box.setWindowTitle("Reboot failed")
        err_box.setText(msg)
        err_box.setStandardButtons(QMessageBox.Ok)
        err_box.setStyleSheet("QLabel { font-size: 18px; } QPushButton { font-size: 18px; }")
        err_box.exec()

    # --------------------------
    # Phase events from logs
    # --------------------------
    def on_phase_event(self, event: str):
        if event == "download_complete":
            # Some systems do not report intermediate progress > 50; this event is the
            # reliable indicator that the download stage has finished.
            self.phase_timer.stop()
            self.phase = "downloaded"
            self._set_progress_floor(95)
            self.info.setText("Download completed. Applying update...")
            self.footer.setText("Finalizing changes...")
            return

        if self.phase == "idle":
            return

        if event == "install_started":
            self.switch_to_installing()
            return

        if event == "need_reboot":
            # NEED_COMPLETION means installation is done but reboot is required to apply it.
            self.phase = "need_reboot"
            self.phase_timer.stop()
            self.switch_to_installing()
            self._lock_ui(False)

            self.info.setText("Update ready. Reboot required.")
            self.footer.setText("Please reboot to complete the update.")
            self._show_reboot_prompt()
            return

        if event == "install_complete":
            self.switch_to_installing()
            return

        if event == "rebooting":
            # Some log streams emit a rebooting line before the actual reboot request.
            self.info.setText("Rebooting to apply the update...")
            self.footer.setText("Please wait while the device restarts.")
            return

    def on_network_activity(self, kbps: float):
        # Purely informational. The update flow can continue without this metric.
        if not self._is_update_flow_active():
            return

        if kbps < 1.0:
            txt = "Network activity: < 1 KB/s"
        elif kbps < 1024.0:
            txt = f"Network activity: {int(kbps)} KB/s"
        else:
            mbps = kbps / 1024.0
            txt = f"Network activity: {mbps:.1f} MB/s"

        self.net_label.setText(txt)

    def on_error(self, err: str):
        # Worker errors are surfaced to the user.
        # Typical reasons: D-Bus not reachable, log file not mounted, permissions issues.
        self.info.setText("Failed to connect to D-Bus / Aktualizr.")
        self.footer.setText(err)
        self.reset_progress_state()
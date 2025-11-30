import sys
import queue
import threading
import os
import json
import datetime
import time

import zmq
import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters as exporters
from PyQt6 import QtWidgets, QtCore, QtGui

# --- Configuration ---
# Must match the PUB ports in reducer.py
REDUCER_STXM_ADDR = "tcp://127.0.0.1:5556"
REDUCER_FRAME_ADDR = "tcp://127.0.0.1:5557"
REDUCER_MD_ADDR = "tcp://127.0.0.1:5558"


class DataReceiver(threading.Thread):
    """
    A dedicated thread for receiving ZMQ data.
    To move all blocking I/O (network) operations off of the main GUI thread.
    This prevents the application from freezing.
    It connects to the Reducer's PUB sockets and puts received data into thread-safe queues.
    """

    def __init__(self, md_queue, stxm_queue, frame_queue, stop_event):
        super().__init__()
        self.daemon = True  # Allows app to exit even if thread is running
        self.md_queue = md_queue
        self.stxm_queue = stxm_queue
        self.frame_queue = frame_queue
        self.stop_event = stop_event

        # ZMQ setup
        self.context = zmq.Context()
        self.md_socket = self.context.socket(zmq.SUB)
        self.stxm_socket = self.context.socket(zmq.SUB)
        self.frame_socket = self.context.socket(zmq.SUB)

        # Poller
        self.poller = zmq.Poller()

    def run(self):
        """Main thread loop."""
        print("[Visualizer] Starting data receiver thread...")
        try:
            # Connect sockets
            self.md_socket.connect(REDUCER_MD_ADDR)
            self.stxm_socket.connect(REDUCER_STXM_ADDR)
            self.frame_socket.connect(REDUCER_FRAME_ADDR)

            # Subscribe to topics
            self.md_socket.subscribe("metadata")
            self.stxm_socket.subscribe("stxm_data")
            self.frame_socket.subscribe("frame_data")

            # Register sockets with poller
            self.poller.register(self.md_socket, zmq.POLLIN)
            self.poller.register(self.stxm_socket, zmq.POLLIN)
            self.poller.register(self.frame_socket, zmq.POLLIN)

            while not self.stop_event.is_set():
                # Poll with a timeout (100ms). Allows the loop to check self.stop_event
                socks = dict(self.poller.poll(100))

                # --- Case 1: New Metadata ---
                if self.md_socket in socks:
                    self.md_socket.recv_string()
                    md = self.md_socket.recv_json()
                    # Bounded queue to store only the latest
                    self._drain_queue(self.md_queue)
                    self.md_queue.put(md)

                # --- Case 2: New STXM Data Point ---
                if self.stxm_socket in socks:
                    self.stxm_socket.recv_string()
                    data = self.stxm_socket.recv_json()
                    # Unbounded queue to ensure all points are processed
                    self.stxm_queue.put(data)

                # --- Case 3: New Frame Data ---
                if self.frame_socket in socks:
                    self.frame_socket.recv_string()
                    header = self.frame_socket.recv_json(flags=0)
                    buffer = self.frame_socket.recv(copy=False)

                    frame = np.frombuffer(
                        buffer, dtype=header['dtype']
                    ).reshape(header['shape'])

                    # Bounded queue to store only the latest
                    self._drain_queue(self.frame_queue)
                    self.frame_queue.put(frame)

        except Exception as e:
            print(f"[Visualizer] Error in receiver thread: {e}")
        finally:
            self.md_socket.close()
            self.stxm_socket.close()
            self.frame_socket.close()
            self.context.term()
            print("[Visualizer] Receiver thread stopped.")

    def _drain_queue(self, q):
        """Empties a queue."""
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def stop(self):
        """Signals the thread to stop."""
        self.stop_event.set()


class VisualizerWindow(QtWidgets.QMainWindow):
    """
    The main application window.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Live STXM Viewer")
        self.setGeometry(100, 100, 1200, 600)

        if getattr(sys, 'frozen', False):
            # If run as .exe, look in the temp folder
            icon_path = os.path.join(sys._MEIPASS, 'assets', 'icon.ico')
        else:
            # If run as script, look in the assets folder
            icon_path = os.path.join(os.path.dirname(__file__), '..', 'assets', 'icon.ico')

        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))

        # Data Storage
        self.stxm_map_data = None
        self.scan_shape = None
        self.current_metadata = None
        self.total_frames = 0
        self.auto_save_enabled = False
        self.pixel_info_enabled = False
        self.first_scan_logged = False

        # Direction Flags
        self.invert_x = False
        self.invert_y = False

        # Activity Tracking for Watchdog
        self.last_activity_time = time.time()
        self.is_in_standby = False

        # --- Queues ---
        # Three queues to pass data from the worker thread to this main GUI thread.
        # Bridge that decouples the threads.
        self.md_queue = queue.Queue(maxsize=1)
        self.stxm_queue = queue.Queue()
        self.frame_queue = queue.Queue(maxsize=1)

        # --- Thread Management ---
        self.stop_event = threading.Event()
        self.receiver_thread = DataReceiver(
            self.md_queue, self.stxm_queue, self.frame_queue, self.stop_event
        )

        # --- Setup the GUI ---
        self._setup_ui()
        self.receiver_thread.start()

        # --- The GUI Heartbeat ---
        # Instead of updating the GUI 500+ times/sec, updating it at a smooth, human-visible rate.
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_plots)
        self.timer.start(33) # Approx. 30 fps

        # Watchdog Timer (Checks every 3 second)
        self.watchdog_timer = QtCore.QTimer()
        self.watchdog_timer.timeout.connect(self.check_activity)
        self.watchdog_timer.start(3000)

    def _setup_ui(self):
        """Creates the widgets and layout."""
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        layout = QtWidgets.QHBoxLayout()
        central_widget.setLayout(layout)

        # --- STXM Map View ---
        stxm_panel = QtWidgets.QVBoxLayout()
        stxm_label = QtWidgets.QLabel("STXM Map View")
        stxm_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.stxm_view = pg.ImageView(name="STXMMap")

        stxm_panel.addWidget(stxm_label)
        stxm_panel.addWidget(self.stxm_view)

        self.stxm_view.getView().invertY(False) # Fixing the issue with Y axis

        # Add crosshairs
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen='g')
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen='g')
        self.stxm_view.addItem(self.v_line, ignoreBounds=True)
        self.stxm_view.addItem(self.h_line, ignoreBounds=True)
        self.v_line.hide()  # HIDE by default
        self.h_line.hide()  # HIDE by default

        # --- STXM Controls Layout ---
        # Using a grid layout
        stxm_controls_layout = QtWidgets.QGridLayout()

        # Add a label for pixel info
        self.pixel_info_checkbox = QtWidgets.QCheckBox("Show Pixel Info")
        stxm_controls_layout.addWidget(self.pixel_info_checkbox, 0, 0)
        self.pixel_info_label = QtWidgets.QLabel("Info: Waiting...")
        stxm_controls_layout.addWidget(self.pixel_info_label, 0, 1)
        self.pixel_info_label.hide()

        # --- Add Colormap Selector ---
        cmap_label = QtWidgets.QLabel("Colormap:")
        stxm_controls_layout.addWidget(cmap_label, 1, 0)
        self.cmap_combo = QtWidgets.QComboBox()
        # Map for "Friendly Name" -> "Internal Name"
        self.colormaps = {} # Store loaded colormap object
        pos = np.array([0.0, 1.0])
        color = np.array([[0, 0, 0, 255], [255, 255, 255, 255]], dtype=np.ubyte)
        self.colormaps['Grayscale (Default)'] = pg.ColorMap(pos, color)
        self.cmap_combo.addItem('Grayscale (Default)')

        # More colormap can be found here
        # https://matplotlib.org/stable/gallery/color/colormap_reference.html
        self.cmap_name_map = {'Cividis': 'cividis', 'Magma': 'magma', 'Plasma': 'plasma', 'Viridis': 'viridis'}
        for friendly, internal in self.cmap_name_map.items():
            try:
                self.colormaps[internal] = pg.colormap.get(internal)
                self.cmap_combo.addItem(friendly)
            except FileNotFoundError:
                print(f"[Visualizer] WARN: Could not find colormap file '{internal}'. Skipping.")

        stxm_controls_layout.addWidget(self.cmap_combo, 1, 1)  # Row 1, Col 1

        # --- Save Controls ---
        self.auto_save_checkbox = QtWidgets.QCheckBox("Auto-save all scans")
        stxm_controls_layout.addWidget(self.auto_save_checkbox, 2, 0)  # Row 2, Col 0

        self.save_button = QtWidgets.QPushButton("Save Current Scan Data")
        self.save_button.setEnabled(False)
        stxm_controls_layout.addWidget(self.save_button, 2, 1)  # Row 2, Col 1

        # Add the grid of controls to the STXM panel
        stxm_panel.addLayout(stxm_controls_layout)

        # --- Connect signals for new controls ---
        self.save_button.clicked.connect(lambda: self.save_scan(triggered_by_button=True))
        self.auto_save_checkbox.toggled.connect(self.toggle_auto_save)
        self.pixel_info_checkbox.toggled.connect(self.toggle_pixel_info)
        self.stxm_view.scene.sigMouseMoved.connect(self.mouse_moved_stxm)
        self.cmap_combo.currentTextChanged.connect(self.update_colormap)

        # Customize STXM view properties
        # self.stxm_view.ui.histogram.hide() # Uncomment to remove histogram
        self.stxm_view.ui.roiBtn.hide()
        self.stxm_view.ui.menuBtn.hide()

        # --- Frame View ---
        frame_panel = QtWidgets.QVBoxLayout()
        frame_label = QtWidgets.QLabel("Live Frame View")
        frame_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_view = pg.ImageView(name="FrameView")
        frame_panel.addWidget(frame_label)
        frame_panel.addWidget(self.frame_view)

        # --- Metadata Display ---
        metadata_label = QtWidgets.QLabel("Current Scan Metadata")
        metadata_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.metadata_display = QtWidgets.QTextEdit()
        self.metadata_display.setReadOnly(True)
        self.metadata_display.setText("Waiting for new scan...")
        self.metadata_display.setFixedHeight(150) # Increase to increase size of Current Scan Metadata
        frame_panel.addWidget(metadata_label)
        frame_panel.addWidget(self.metadata_display)

        # -- Progress Bar --
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFormat("[%v/%m] (%p%)")
        self.progress_bar.setValue(0)
        self.progress_bar.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        frame_panel.addWidget(self.progress_bar)
        self.progress_bar.hide()  # Hiding until a scan starts

        # Customize Frame view properties
        #  self.frame_view.ui.histogram.hide() # Uncomment to remove histogram
        self.frame_view.ui.roiBtn.hide()
        self.frame_view.ui.menuBtn.hide()

        # --- Final Layout ---
        layout.addLayout(frame_panel, stretch=1)
        layout.addLayout(stxm_panel, stretch=1)

    def check_activity(self):
        """Checks if we have received data recently."""
        # 15 second timeout
        if (time.time() - self.last_activity_time > 15.0) and not self.is_in_standby:
            self.enter_standby()

    def enter_standby(self):
        """Resets the UI to a waiting state."""
        if self.is_in_standby:
            return  # Already in standby

        print("[Visualizer] No data received for 15s. Entering Standby.")
        self.is_in_standby = True
        self.setWindowTitle("Live STXM Viewer - STANDBY")
        self.progress_bar.hide()
        self.save_button.setEnabled(False)

        # Prepend STANDBY message
        standby_msg = "--- STANDBY MODE (Waiting for scan) ---\n"

        # Insert the message at the very beginning of the QTextEdit
        self.metadata_display.setText(standby_msg + self.metadata_display.toPlainText())
        # Ensure the top is visible
        self.metadata_display.ensureCursorVisible()

    def update_plots(self):
        """
        Heartbeat function called by the QTimer.
        It drains the queues and updates the plots.
        """

        # 1. Check for New Metadata
        try:
            md = self.md_queue.get_nowait()
            self.handle_metadata(md)
            # Reset activity timer
            self.last_activity_time = time.time()
            self.is_in_standby = False
            self.setWindowTitle("Live STXM Viewer")
        except queue.Empty:
            pass

        # 2. Process ALL Queued STXM Data Points
        # Loop until the STXM queue is empty, updating the data array in memory.
        stxm_map_was_updated = False
        while not self.stxm_queue.empty():
            try:
                data = self.stxm_queue.get_nowait()
                self.handle_stxm_data(data)
                stxm_map_was_updated = True
                # Reset activity timer
                self.last_activity_time = time.time()
            except queue.Empty:
                break

        # Check both, data arrived AND self.stxm_map_data has been initialized by the metadata.
        if stxm_map_was_updated and self.stxm_map_data is not None:
            # Creating a masked array.
            # Tell numpy to "ignore" all pixels that are still equal to 0 (unscanned value).
            masked_data = np.ma.masked_equal(self.stxm_map_data, 0)
            self.stxm_view.setImage(masked_data.T, autoRange=False, autoLevels=True)

        # 3. Process Frame
        try:
            frame = self.frame_queue.get_nowait()
            self.handle_frame(frame)
            self.last_activity_time = time.time()
        except queue.Empty:
            pass

    def handle_metadata(self, md):
        """Called when new scan metadata is received."""
        print(f"[Visualizer] Received new metadata: {md}")

        # If there is old data sitting in the stxm_queue from a previous aborted run,
        # it must be thrown away before starting new scan.
        dropped_packets = 0
        while not self.stxm_queue.empty():
            try:
                self.stxm_queue.get_nowait()
                dropped_packets += 1
            except queue.Empty:
                break
        if dropped_packets > 0:
            print(f"[Visualizer] Flushed {dropped_packets} stale packets from queue.")

        # Determine Scan Direction
        self.invert_x = md['x_start'] > md['x_stop']
        self.invert_y = md['y_start'] > md['y_stop']
        # direction_msg = f"Scan Direction: X={'Rev' if self.invert_x else 'Fwd'}, Y={'Fwd' if self.invert_y else 'Rev'}"

        # Log to UI
        try:
            now = datetime.datetime.now()
            time_string = now.strftime("%H:%M:%S")
            header = f"--- Scan at {time_string} ---"
            md_string = (
                f"Scan Dimensions:\n"
                f"  X: {md['x_num']} steps (from {md['x_start']:.3f} to {md['x_stop']:.3f})\n"
                f"  Y: {md['y_num']} steps (from {md['y_start']:.3f} to {md['y_stop']:.3f})\n"
                f"\n"
                f"Detector:\n"
                f"  Shape: {md['detector_shape'][0]} x {md['detector_shape'][1]}\n"
                f"  Exposure: {md['exposure_time_s']:.4f} s\n"
               # f"{direction_msg}\n"
            )
            new_entry = header + "\n" + md_string

            if not self.first_scan_logged:
                self.metadata_display.setText(new_entry)
                self.first_scan_logged = True
            else:
                # Prepend new entry to the top
                current_text = self.metadata_display.toPlainText()
                self.metadata_display.setText(new_entry + "\n" + current_text)

                # Scroll to top to ensure visibility
                self.metadata_display.moveCursor(QtGui.QTextCursor.MoveOperation.Start)

        except KeyError as e:
            self.metadata_display.append(f"Error: Missing key in metadata '{e}'")
        except Exception as e:
            self.metadata_display.append(f"Error parsing metadata:\n{e}")

        # Use metadata to create the 2D numpy array that will store STXM map.
        self.current_metadata = md
        self.scan_shape = (md['x_num'], md['y_num'])
        self.total_frames = md['x_num'] * md['y_num']

        # Create a new data array
        self.stxm_map_data = np.zeros(self.scan_shape, dtype=np.float32)

        # Reset and show progress bar
        self.progress_bar.setMaximum(self.total_frames)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("[%v/%m] (%p%)")
        self.progress_bar.show()

        self.save_button.setEnabled(False)
        self.save_button.setText("Save Current Scan Data")

        self.stxm_view.setImage(self.stxm_map_data.T, autoRange=True, autoLevels=True)

    def handle_stxm_data(self, data):
        """
        Called to update a single pixel in the STXM map.
        This function just updates the data array in memory.
        """
        if self.stxm_map_data is None:
            return

        # Calculate the (x, y) coordinate from the 1D frame_index.
        index = data['index']
        intensity = data['intensity']

        # Guard against indices larger than current map
        # (Should happen rarely as queue is flushed, for good safety)
        if index >= self.total_frames:
            return

        # Calculate Sequence Coords
        seq_x = index % self.scan_shape[0]
        seq_y = index // self.scan_shape[0]

        # Apply Spatial Inversion
        if self.invert_x:
            spatial_x = (self.scan_shape[0] - 1) - seq_x
        else:
            spatial_x = seq_x

        if self.invert_y:
            spatial_y = (self.scan_shape[1] - 1) - seq_y
        else:
            spatial_y = seq_y

        # 3. Update Map
        if spatial_y < self.scan_shape[1]:
            self.stxm_map_data[spatial_x, spatial_y] = intensity

            current_progress = index + 1
            self.progress_bar.setValue(current_progress)

            # Check if scan is complete
            if current_progress == self.total_frames:
                self.progress_bar.setFormat("Scan Complete! [%v/%m] (100%)")
                self.save_button.setEnabled(True)

                if self.auto_save_enabled:
                    print("[Visualizer] Auto-saving scan (Scan Complete)...")
                    self.save_scan(triggered_by_button=False)

    def handle_frame(self, frame):
        """Displays the latest detector frame."""
        self.frame_view.setImage(
            frame,
            autoRange=False,
            autoLevels=False
        )

    def closeEvent(self, event):
        """
        Called when the user clicks the 'X' button.
        Critical for a clean shutdown.
        """
        print("[Visualizer] Closing application...")
        self.timer.stop()
        self.watchdog_timer.stop()  # Stop the watchdog
        self.stop_event.set()
        self.receiver_thread.join(timeout=2.0)
        event.accept()

    def save_scan(self, triggered_by_button=False):
        """Saves the current STXM map and metadata to a 'saved_data' folder."""
        if self.stxm_map_data is None or self.current_metadata is None:
            print("[Visualizer] No data to save.")
            return

        save_dir = "saved_data"
        os.makedirs(save_dir, exist_ok=True)

        # Create a unique filename based on the current time
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        basename = f"stxm_scan_{ts}"
        filepath_base = os.path.join(save_dir, basename)

        print(f"[Visualizer] Saving scan to {filepath_base}...")

        try:
            # Save the raw data as a NumPy file
            np.save(f"{filepath_base}_data.npy", self.stxm_map_data)

            # Save the metadata as a JSON file
            with open(f"{filepath_base}_meta.json", 'w') as f:
                json.dump(self.current_metadata, f, indent=4)

            # Save a quick-look PNG of the view
            exporter = exporters.ImageExporter(self.stxm_view.getImageItem())
            exporter.export(f"{filepath_base}_image.png")

            print(f"[Visualizer] Save complete.")

            if triggered_by_button:
                self.save_button.setText("Saved!")
                QtCore.QTimer.singleShot(2000, lambda: self.save_button.setText("Save Current Scan Data"))
        except Exception as e:
            print(f"[Visualizer] ERROR saving scan: {e}")

    def toggle_auto_save(self, checked):
        """Called when the auto-save checkbox is toggled."""
        self.auto_save_enabled = checked
        if checked:
            print("[Visualizer] Auto-save enabled.")
        else:
            print("[Visualizer] Auto-save disabled.")

    # --- Toggle pixel info ---
    def toggle_pixel_info(self, checked):
        """Called when the pixel info checkbox is toggled."""
        self.pixel_info_enabled = checked
        self.v_line.setVisible(checked)
        self.h_line.setVisible(checked)
        self.pixel_info_label.setVisible(checked)
        if not checked:
            # Reset label when hiding
            self.pixel_info_label.setText("Info: (X, Y) = ---, Value = ---")

    def mouse_moved_stxm(self, pos):
        """Updates the tooltip with grid coordinates, physical coordinates, and intensity."""
        if not self.pixel_info_enabled:
            return

        # Check if mouse is inside the image and we have valid data
        if self.stxm_view.getImageItem().sceneBoundingRect().contains(
                pos) and self.stxm_map_data is not None and self.current_metadata is not None:
            mouse_point = self.stxm_view.getImageItem().mapFromScene(pos)
            x_idx = int(mouse_point.x())
            y_idx = int(mouse_point.y())

            # Update Crosshairs position
            self.v_line.setPos(mouse_point.x())
            self.h_line.setPos(mouse_point.y())

            # Check if the index is valid
            if 0 <= x_idx < self.scan_shape[0] and 0 <= y_idx < self.scan_shape[1]:
                val = self.stxm_map_data[x_idx, y_idx]

                # Calculate Physical Coordinates
                md = self.current_metadata

                # Use min/max because our display is always strictly Low -> High
                x_min = min(md['x_start'], md['x_stop'])
                x_max = max(md['x_start'], md['x_stop'])

                # Avoid division by zero if scan is a single point (unlikely but safe)
                x_denom = (self.scan_shape[0] - 1) if self.scan_shape[0] > 1 else 1
                y_denom = (self.scan_shape[1] - 1) if self.scan_shape[1] > 1 else 1

                x_phys = x_min + (x_idx / x_denom) * (x_max - x_min)

                y_min = min(md['y_start'], md['y_stop'])
                y_max = max(md['y_start'], md['y_stop'])
                y_phys = y_min + (y_idx / y_denom) * (y_max - y_min)

                self.pixel_info_label.setText(
                    f"Grid: ({x_idx}, {y_idx}) | Position: ({x_phys:.3f}, {y_phys:.3f}) | Val: {val:.2f}"
                )

    def update_colormap(self, friendly_name):
        """Applies the selected colormap to the STXM view."""
        internal_name = self.cmap_name_map.get(friendly_name)

        if internal_name and internal_name in self.colormaps:
            self.stxm_view.setColorMap(self.colormaps[internal_name])
        elif friendly_name in self.colormaps:
            self.stxm_view.setColorMap(self.colormaps[friendly_name])
        else:
            # Should not happen if the dropdown is populated correctly
            print(f"[Visualizer] Could not find loaded colormap for '{friendly_name}'")

def main():
    """
    Main entry point for the Visualizer.
    This is the function imported by main.py
    """
    # Set pyqtgraph config
    pg.setConfigOption('imageAxisOrder', 'row-major')  # 'row-major' = (y, x)

    app = QtWidgets.QApplication(sys.argv)
    window = VisualizerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
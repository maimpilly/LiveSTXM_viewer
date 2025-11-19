# Architecture Notes and Technical Deep Dive

This document details the core design and technical solutions implemented in the LiveSTXM Viewer project.

## System Decoupling (The Scalability Solution)

The fundamental challenge of this project was protecting the graphical user interface (GUI) from being overwhelmed by the high-speed data stream. The system allows for receiving data at rates of hundreds of frames per second without crashing or freezing the UI.

### ZeroMQ (0MQ) and the Three-Process Architecture:
The system is built on a **Producer-Consumer model** using three dedicated Python processes communicating via 0MQ sockets. This architecture bypasses the Python **Global Interpreter Lock (GIL)**, allowing for true parallelism.

1.  **`emulator.py` (Data Source):** Produces metadata and raw frames.
2.  **`src/receiver.py` (The Buffer):** A dedicated forwarder that handles the high-speed ingress of raw data. By isolating the network subscription here, we prevent the processing logic from blocking the network socket.
3.  **`src/reducer.py` (Processing Core):** Offloads heavy computation. It calculates the scalar intensity (sum of all pixels) for the STXM map and downsamples the large detector frames.
4.  **`src/visualizer.py` (The Consumer):** Receives already-processed data for rapid display.

**Concurrency and Frame Skipping:**
To ensure the application runs smoothly on standard office-grade laptops (low resource usage), the Visualizer employs a **multithreaded approach**:
* **Data Reception Thread:** Dedicated exclusively to blocking network I/O.
* **Main UI Thread:** Dedicated exclusively to rendering via a non-blocking timer.
* **Bounded Queues:** The frame queue has a maximum size of 1. This ensures that if the data rate exceeds the display rate (e.g., 30 fps), intermediate frames are automatically dropped while the most recent frame is always displayed. This guarantees the interface remains responsive.

## Functional Implementation

### Live Visualization Views
The application launches from a single script (`main.py`) and presents two distinct, synchronized views:
* **Live Frame View:** Displays the 2D detector image. This view is updated at a regular, non-blocking interval (approx. 30 fps) using `pyqtgraph`'s optimized rendering, including automatic downsampling for large (4000x4000) arrays.
* **Live STXM Map View:** A 2D raster map that updates progressively. For every detector frame received, the system calculates the sum of intensities and places that scalar value at the specific (X, Y) scan position defined in the metadata.

### Coordinate Transformation (Fixed X/Y Coordinates)
The system ensures the STXM display has a fixed X/Y direction regardless of the motor's travel path. For example, whether scanning x<sub>start</sub> to x<sub>stop</sub>, forward or reverse).

**Solution (Spatial Inversion):**
The `handle_stxm_data` logic explicitly calculates the final spatial index before updating the array.
* The code calculates the sequential index based on arrival order.
* It checks the direction flags (`self.invert_x`, `self.invert_y`).
* If the scan is inverted, the code flips the data mathematically before placing it in the NumPy array.
* This ensures the display always renders Cartesian coordinates correctly, thereby providing a fixed coordinate system.

## Robustness and Error Handling

* **Startup Synchronization:** The system is designed to handle the specific startup sequence where metadata must be received before frame data. The visualization pipeline waits for the initial configuration message before attempting to render, creating a robust startup state.
* **Clean Shutdown:** The `main.py` launcher monitors the Visualizer process. When the UI is closed by the user, a signal is sent to terminate the Receiver and Reducer processes cleanly, preventing orphaned background tasks.
* **Watchdog Timer:** A dedicated timer checks for activity every 1 second. If the stream is inactive for more than 15 seconds, the application enters a "Standby" mode, alerting the user that the data stream has stopped.
* **Zombie Data Flush:** Upon receiving new metadata, the system flushes the internal queues. This prevents data from a previous, aborted scan from corrupting the visualization of a new scan.
import zmq
import numpy as np

# Input from Receiver
RECEIVER_QUEUE_ADDR = "tcp://127.0.0.1:5555"

# Input from Simulator
SIM_MD_HOST = "127.0.0.1"
SIM_MD_PORT = 50001

# Outputs to GUI
GUI_STXM_ADDR = "tcp://127.0.0.1:5556"  #
GUI_FRAME_ADDR = "tcp://127.0.0.1:5557" # Publish Frames
GUI_MD_ADDR = "tcp://127.0.0.1:5558"  # Forwarding metadata

DOWNSAMPLE_FACTOR = 10


def main():
    """
    PULLs raw frames from the Receiver, performs calculations (sum and downsample), and PUBlishes the reduced data
    to the Visualizer.
    This process is now the only process besides the Receiver that subscribes to the simulator. It forwards all data,
    including metadata, to the GUI.
    """
    print(f"[Reducer] Starting...")

    context = zmq.Context()

    # --- Input Socket (from Receiver) ---
    receiver_socket = context.socket(zmq.PULL)
    receiver_socket.connect(RECEIVER_QUEUE_ADDR)
    print(f"[Reducer] PULLing data from {RECEIVER_QUEUE_ADDR}")

    # --- Input Socket (from Simulator Metadata) ---
    md_socket = context.socket(zmq.SUB)
    md_socket.connect(f"tcp://{SIM_MD_HOST}:{SIM_MD_PORT}")
    md_socket.subscribe(b"")
    print(f"[Reducer] Subscribed to METADATA at tcp://{SIM_MD_HOST}:{SIM_MD_PORT}")

    # --- Output Sockets (to GUI) ---
    stxm_pub_socket = context.socket(zmq.PUB)
    stxm_pub_socket.bind(GUI_STXM_ADDR)
    print(f"[Reducer] Publishing STXM data to {GUI_STXM_ADDR}")

    frame_pub_socket = context.socket(zmq.PUB)
    frame_pub_socket.bind(GUI_FRAME_ADDR)
    print(f"[Reducer] Publishing FRAME data to {GUI_FRAME_ADDR}")

    # --- Metadata Output Socket (to GUI) ---
    md_pub_socket = context.socket(zmq.PUB)
    md_pub_socket.bind(GUI_MD_ADDR)
    print(f"[Reducer] Publishing METADATA to {GUI_MD_ADDR}")

    # --- Poller for Multiple Inputs ---
    poller = zmq.Poller()
    poller.register(receiver_socket, zmq.POLLIN)
    poller.register(md_socket, zmq.POLLIN)

    frame_index = 0
    print("[Reducer] Running.")

    try:
        while True:
            socks = dict(poller.poll())

            # --- Case 1: New Metadata Arrives ---
            if md_socket in socks:
                md = md_socket.recv_json()
                print("[Reducer] === New Scan DETECTED === Resetting index to 0")
                frame_index = 0

                # --- NEW: Forward metadata to GUI ---
                # Publish the received metadata on the new topic.
                md_pub_socket.send_string("metadata", zmq.SNDMORE)
                md_pub_socket.send_json(md)

            # --- Case 2: New Frame Data Arrives ---
            if receiver_socket in socks:
                header = receiver_socket.recv_json()
                buffer = receiver_socket.recv(copy=False)

                frame = np.frombuffer(
                    buffer,
                    dtype=header['dtype']
                ).reshape(header['shape'])

                intensity = np.sum(frame)
                downsampled_frame = frame[::DOWNSAMPLE_FACTOR, ::DOWNSAMPLE_FACTOR].copy()

                # Send STXM data
                stxm_data = {
                    'index': frame_index,
                    'intensity': float(intensity)
                }
                stxm_pub_socket.send_string("stxm_data", zmq.SNDMORE)
                stxm_pub_socket.send_json(stxm_data)

                # Send Frame data
                frame_header = {
                    'dtype': str(downsampled_frame.dtype),
                    'shape': downsampled_frame.shape
                }
                frame_pub_socket.send_string("frame_data", zmq.SNDMORE)
                frame_pub_socket.send_json(frame_header, zmq.SNDMORE)
                frame_pub_socket.send(downsampled_frame, copy=False)

                frame_index += 1

    except KeyboardInterrupt:
        print("\n[Reducer] Shutting down.")
    finally:
        receiver_socket.close()
        md_socket.close()
        stxm_pub_socket.close()
        frame_pub_socket.close()
        md_pub_socket.close()
        context.term()
        print("[Reducer] Stopped.")


if __name__ == "__main__":
    main()
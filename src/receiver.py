import zmq

SIM_DATA_HOST = "127.0.0.1"
SIM_DATA_PORT = 50002

# This is an internal queue.
REDUCER_QUEUE_ADDR = "tcp://127.0.0.1:5555"

def main():
    """
    Connects to the simulator's DATA stream and forwards
    every received frame to the Reducer process via an
    internal PUSH/PULL queue.
    """
    print(f"[Receiver] Starting...")

    context = zmq.Context()

    # --- Input Socket (from Simulator) ---
    # SUB socket connects to the simulator's PUB.
    sim_socket = context.socket(zmq.SUB)
    sim_socket.connect(f"tcp://{SIM_DATA_HOST}:{SIM_DATA_PORT}")
    sim_socket.subscribe(b"")
    print(f"[Receiver] Subscribed to DATA at tcp://{SIM_DATA_HOST}:{SIM_DATA_PORT}")

    # --- Output Socket (to Reducer) ---
    # A PUSH socket. PUSH/PULL is a load-balancing queue. The Receiver can 'PUSH' data as fast as it arrives, and the
    # Reducer can 'PULL' it as fast as it can process it.
    reducer_socket = context.socket(zmq.PUSH)
    reducer_socket.bind(REDUCER_QUEUE_ADDR)
    print(f"[Receiver] PUSHing data to {REDUCER_QUEUE_ADDR}")

    print("[Receiver] Running.")
    try:
        while True:
            # Receive the two-part message from the simulator
            header = sim_socket.recv_json()
            buffer = sim_socket.recv(copy=False)

            # Forward both parts to the Reducer.
            # SNDMORE to send it as a multi-part message so the Reducer gets them together.
            reducer_socket.send_json(header, zmq.SNDMORE)
            reducer_socket.send(buffer, copy=False)

    except KeyboardInterrupt:
        print("\n[Receiver] Shutting down.")
    finally:
        sim_socket.close()
        reducer_socket.close()
        context.term()
        print("[Receiver] Stopped.")


if __name__ == "__main__":
    main()
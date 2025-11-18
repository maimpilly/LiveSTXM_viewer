import multiprocessing
import time
import sys

def main():
    """
    Launches the Receiver, Reducer, and Visualizer processes.
    This script monitors the Visualizer process and shuts down all other processes when the GUI is closed.
    """
    try:
        from src.receiver import main as receiver_main
        from src.reducer import main as reducer_main
        from src.visualizer import main as visualizer_main
    except ImportError as e:
        print("\n--- FATAL IMPORT ERROR ---", file=sys.stderr)
        print("Error: Could not import receiver, reducer, or visualizer.", file=sys.stderr)
        print("This is likely due to a syntax error in one of those files.", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    print("Starting LiveSTXM application...")

    # --- Create process objects ---
    p_receiver = multiprocessing.Process(target=receiver_main, name="Receiver")
    p_reducer = multiprocessing.Process(target=reducer_main, name="Reducer")
    p_visualizer = multiprocessing.Process(target=visualizer_main, name="Visualizer")

    try:
        # Start processes
        print("Starting Receiver...")
        p_receiver.start()
        print("Starting Reducer...")
        p_reducer.start()
        print("Starting Visualizer...")
        p_visualizer.start()

        print("\nInitializing sockets... (Waiting 2.0s)")
        time.sleep(2.0)

        print("\n[--- READY ---]")
        print("You can now start the emulate_data_stream.py simulator.")

        # --- Main application loop ---
        # Poll the visualizer process to see if it's alive.
        # This is a signal to shut down.
        # When the user closes the GUI, p_visualizer.is_alive() will become False, and breaks the loop.
        while p_visualizer.is_alive():
            time.sleep(0.5)

        print("Visualizer process has exited. Shutting down...")

    except KeyboardInterrupt:
        print("\nCaught KeyboardInterrupt. Shutting down main application...")
    finally:
        # --- Clean shutdown ---
        # Terminate all processes cleanly in reverse order.
        print("Terminating processes...")
        if p_visualizer.is_alive():
            print("Terminating Visualizer...")
            p_visualizer.terminate()
            p_visualizer.join(1.0)

        if p_reducer.is_alive():
            print("Terminating Reducer...")
            p_reducer.terminate()
            p_reducer.join(1.0)

        if p_receiver.is_alive():
            print("Terminating Receiver...")
            p_receiver.terminate()
            p_receiver.join(1.0)

        print("LiveSTXM application shut down.")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
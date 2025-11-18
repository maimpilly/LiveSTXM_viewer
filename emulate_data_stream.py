import timeit
from dataclasses import dataclass, asdict
from threading import Event
from time import sleep

import numba as nb
import numpy as np
import zmq


MIN_STEPS = 10
MAX_STEPS = 20
MIN_DWELL_TIME = 1e-3


@dataclass
class ScanMD:
    x_start: float
    x_stop: float
    x_num: int
    y_start: float
    y_stop: float
    y_num: int
    exposure_time_s: float
    detector_shape: tuple[int, int]


@nb.njit("float32[:,:](int_, int_)", parallel=True)
def nb_rand(n, m):
    res = np.empty((n, m), dtype=np.float32)
    # Parallel loop
    for i in nb.prange(n):
        for j in range(m):
            res[i, j] = np.random.rand()
    return res


class PublisherEmulator:
    def __init__(
        self,
        detector_shape: tuple[int, int],
        md_host: str = "127.0.0.1",
        md_port: int = 50001,
        data_host: str = "127.0.0.1",
        data_port: int = 50002,
        sleep_between_scans_s: int = 5,
    ):
        self.detector_shape = detector_shape
        self.md_host = md_host
        self.md_port = md_port
        self.data_host = data_host
        self.data_port = data_port

        self.sleep_between_scans_s = sleep_between_scans_s

        self.stopped = Event()
        self.md_pub_sock = zmq.Context.instance().socket(zmq.PUB)
        self.data_pub_sock = zmq.Context.instance().socket(zmq.PUB)
        self.connect()

    def connect(self) -> None:
        self.md_pub_sock.bind(f"tcp://{self.md_host}:{self.md_port}")
        self.data_pub_sock.bind(f"tcp://{self.data_host}:{self.data_port}")
        # --- FIX added by Sarath---
        # Giving subscribers (Reducer) a moment to connect before sending the first message.
        print("[Emulator] Sockets bound. Waiting 1s for subscribers...")
        sleep(1.0)
        # --- END OF FIX ---

    def run(self) -> None:
        self._publish()

    def _generate_data_slow(self) -> np.ndarray:
        random_frame = np.random.rand(*self.detector_shape)
        return random_frame

    def _generate_data_fast(self) -> np.ndarray:
        # Generate random data faster with numba and float32
        random_frame = nb_rand(*self.detector_shape)
        return random_frame

    def _generate_md(self) -> ScanMD:
        x_start, x_stop, x_num = self._generate_axis_md()
        y_start, y_stop, y_num = self._generate_axis_md()
        exposure_time_s = max(MIN_DWELL_TIME, np.random.rand())
        return ScanMD(
            x_start=x_start,
            x_stop=x_stop,
            x_num=x_num,
            y_start=y_start,
            y_stop=y_stop,
            y_num=y_num,
            exposure_time_s=exposure_time_s,
            detector_shape=self.detector_shape,
        )

    @staticmethod
    def _generate_axis_md() -> tuple[float, float, int]:
        start = np.random.rand()
        step = np.random.rand()
        num = np.random.randint(low=MIN_STEPS, high=MAX_STEPS)
        invert_mult = np.random.choice(
            [-1, 1]
        )  # axis can be scanned in either direction
        stop = start + step * invert_mult * num
        return start, stop, num

    def _send_data(self, data_array: np.ndarray):
        md = dict(
            dtype=str(data_array.dtype),
            shape=data_array.shape,
        )
        self.data_pub_sock.send_json(md, zmq.SNDMORE)
        self.data_pub_sock.send(data_array, copy=False)

    def _publish(self):
        while not self.stopped.is_set():
            scan_md = self._generate_md()
            print(scan_md)
            self.md_pub_sock.send_json(asdict(scan_md))
            ns = globals()
            ns["self"] = self
            gen_time_s = timeit.timeit(
                f"self._generate_data_fast()", globals=ns, number=10
            )
            sleep_between_frames_s = max(0.0, scan_md.exposure_time_s - gen_time_s / 10)
            for i in range(scan_md.x_num * scan_md.y_num):
                if self.stopped.is_set():
                    return
                print(f"Send frame {i}")
                frame_data = self._generate_data_fast()
                self._send_data(data_array=frame_data)
                sleep(sleep_between_frames_s)

            if self.stopped.is_set():
                return
            sleep(self.sleep_between_scans_s - gen_time_s)

    def close(self):
        self.stopped.set()


if __name__ == "__main__":
    publisher = PublisherEmulator(detector_shape=(200, 200))  # Frames can be as large as 4000x4000
    try:
        publisher.run()
    except KeyboardInterrupt:
        publisher.close()

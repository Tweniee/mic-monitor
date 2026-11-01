#!/usr/bin/env python3
"""
Mic Monitor: see every connected (Bluetooth) mic live, click one to listen to only that mic.

Audio is read directly from macOS Core Audio with `sounddevice` (PortAudio), so it does
not depend on browser mic permissions. A small local page (http://localhost:8765) is the
screen: it only shows levels and sends clicks back to Python.

    python3 mic_monitor.py            # run it
    python3 mic_monitor.py --list     # just print the mics Python can see
    python3 mic_monitor.py --demo     # fake mics, no audio hardware needed
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import platform
import random
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_MICS = 5
ACTIVE_THRESHOLD = 0.015   # RMS level that counts as "picking up sound"
HOLD_SECONDS = 0.35        # keep a card lit briefly after sound stops
MAX_BUFFER_SECONDS = 0.25  # cap on playback latency

np = None  # imported lazily so --demo works without numpy
sd = None


def load_audio_libs():
    global np, sd
    try:
        import numpy as _np
        import sounddevice as _sd
    except ImportError as e:
        sys.exit(
            f"Missing package: {e.name}\n"
            "Install the requirements first:\n"
            "    python3 -m pip install -r requirements.txt\n"
        )
    except OSError as e:  # PortAudio library missing (rare on macOS; wheels bundle it)
        sys.exit(f"Could not load PortAudio: {e}\nOn macOS try: brew install portaudio")
    np, sd = _np, _sd


# --------------------------------------------------------------------------- Bluetooth

def connected_bluetooth_names() -> list[str]:
    """Names of currently connected Bluetooth devices (macOS only, best effort)."""
    if platform.system() != "Darwin":
        return []
    try:
        out = subprocess.run(
            ["system_profiler", "SPBluetoothDataType", "-json"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        data = json.loads(out or "{}")
    except Exception:
        return []

    names: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "device_connected" and isinstance(v, list):
                    for entry in v:
                        if isinstance(entry, dict):
                            names.extend(entry.keys())
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return names


def is_bluetooth(device_name: str, bt_names: list[str]) -> bool:
    d = device_name.lower()
    for b in bt_names:
        b = b.lower()
        if b and (b in d or d in b):
            return True
    # Common Bluetooth mic names when system_profiler gives nothing.
    return any(k in d for k in ("airpods", "bluetooth", "hands-free", "headset", "buds", "beats"))


# --------------------------------------------------------------------------- Audio engine

class Mic:
    def __init__(self, index: int, name: str, rate: float, channels: int, bluetooth: bool):
        self.index = index
        self.name = name
        self.rate = rate
        self.channels = channels
        self.bluetooth = bluetooth
        self.level = 0.0
        self.last_loud = 0.0
        self.stream = None
        self.error: str | None = None

    def to_dict(self, listening: int | None) -> dict:
        now = time.monotonic()
        return {
            "id": self.index,
            "name": self.name,
            "bluetooth": self.bluetooth,
            "level": round(self.level, 4),
            "active": self.error is None and now - self.last_loud < HOLD_SECONDS,
            "listening": self.index == listening,
            "error": self.error,
        }


class Monitor:
    def __init__(self, bluetooth_only: bool, include_builtin: bool, output: str | None, gain: float):
        self.bluetooth_only = bluetooth_only
        self.include_builtin = include_builtin
        self.output_pref = output   # name substring from --output, or None for laptop speakers
        self.gain = gain
        self.out_index: int | None = None
        self.outputs: list[dict] = []
        self.lock = threading.RLock()
        self.mics: list[Mic] = []
        self.listening: int | None = None
        self.out_stream = None
        self.out_rate = 48000.0
        self.out_name = ""
        self.chunks: collections.deque = collections.deque()
        self.buffered = 0
        self.buf_lock = threading.Lock()
        self.message = ""

    # ---- device discovery
    def find_inputs(self) -> list[Mic]:
        bt_names = connected_bluetooth_names()
        found: list[Mic] = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] < 1:
                continue
            name = d["name"]
            bt = is_bluetooth(name, bt_names)
            builtin = "macbook" in name.lower() or "built-in" in name.lower()
            if self.bluetooth_only and not bt:
                continue
            if builtin and not self.include_builtin and not bt:
                continue
            found.append(Mic(i, name, float(d["default_samplerate"]), int(d["max_input_channels"]), bt))
        # Bluetooth first, then everything else, keep system order otherwise.
        found.sort(key=lambda m: (not m.bluetooth, m.index))
        return found[:MAX_MICS]

    # ---- (re)scan: stop everything, re-read the device list, reopen streams
    def scan(self):
        with self.lock:
            self._stop_all()
            # PortAudio caches the device list; re-initialise it to see newly connected devices.
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
            self.mics = self.find_inputs()
            # Open the mics first: a Bluetooth headset switches into "call mode" when its mic
            # opens, and macOS may then move the system output to it. Opening the output
            # afterwards, on an explicitly chosen device, avoids playing into the wrong place.
            for mic in self.mics:
                self._open_input(mic)
            time.sleep(0.5)
            self.outputs = self.list_outputs()
            self._open_output(self.pick_output())
            if self.listening is not None and not any(m.index == self.listening for m in self.mics):
                self.listening = None
            ok = [m for m in self.mics if m.error is None]
            self.message = "" if self.mics else "No mics found"
            print(f"Monitoring {len(ok)} of {len(self.mics)} mic(s):")
            for m in self.mics:
                tag = "Bluetooth" if m.bluetooth else "other"
                print(f"  [{m.index}] {m.name} ({tag}, {int(m.rate)} Hz)" + (f"  ERROR: {m.error}" if m.error else ""))

    def _stop_all(self):
        for m in self.mics:
            if m.stream is not None:
                try:
                    m.stream.stop(); m.stream.close()
                except Exception:
                    pass
                m.stream = None
        self._close_output()

    # ---- output (where the selected mic is played)
    @staticmethod
    def list_outputs() -> list[dict]:
        return [{"id": i, "name": d["name"]} for i, d in enumerate(sd.query_devices())
                if d["max_output_channels"] > 0]

    def pick_output(self) -> int | None:
        names = {o["id"]: o["name"].lower() for o in self.outputs}
        if self.output_pref:
            for i, n in names.items():
                if self.output_pref.lower() in n:
                    return i
            print(f"No output matching '{self.output_pref}', using the laptop speakers.")
        # Default: the laptop's own speakers, so the sound comes out loud on the Mac.
        for i, n in names.items():
            if "speaker" in n and ("macbook" in n or "built-in" in n or "imac" in n):
                return i
        for i, n in names.items():
            if "speaker" in n:
                return i
        try:
            return sd.default.device[1] if sd.default.device[1] >= 0 else None
        except Exception:
            return None

    def _close_output(self):
        if self.out_stream is not None:
            try:
                self.out_stream.stop(); self.out_stream.close()
            except Exception:
                pass
            self.out_stream = None

    def _open_output(self, index: int | None):
        self._close_output()
        try:
            dev = sd.query_devices(index) if index is not None else sd.query_devices(kind="output")
            self.out_index = index
            self.out_rate = float(dev["default_samplerate"])
            self.out_name = dev["name"]
            ch = min(2, int(dev["max_output_channels"])) or 1
            self.out_stream = sd.OutputStream(
                device=index, samplerate=self.out_rate, channels=ch, dtype="float32",
                callback=self._out_cb, latency="low",
            )
            self.out_stream.start()
        except Exception as e:
            self.out_stream = None
            self.out_name = f"unavailable ({str(e).splitlines()[0][:80]})"
        print(f"Output (what you hear): {self.out_name}")

    def set_output(self, index: int):
        with self.lock:
            self.output_pref = None
            self._open_output(index)

    def watchdog(self):
        """Reopen the output if macOS kills it (it can when Bluetooth devices change mode)."""
        while True:
            time.sleep(1.0)
            with self.lock:
                s = self.out_stream
                if s is not None and not s.active:
                    print("Output stream stopped; reopening it.")
                    self._open_output(self.out_index)

    def _open_input(self, mic: Mic):
        try:
            mic.stream = sd.InputStream(
                device=mic.index, channels=min(mic.channels, 2), samplerate=mic.rate,
                dtype="float32", callback=self._make_in_cb(mic), latency="low",
            )
            mic.stream.start()
        except Exception as e:
            mic.stream = None
            mic.error = str(e).split("\n")[0][:120]

    # ---- audio callbacks (run on PortAudio threads)
    def _make_in_cb(self, mic: Mic):
        def cb(indata, frames, t, status):
            x = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
            rms = float(np.sqrt(np.mean(x * x))) if frames else 0.0
            mic.level = rms if rms > mic.level else mic.level * 0.85 + rms * 0.15
            if rms > ACTIVE_THRESHOLD:
                mic.last_loud = time.monotonic()
            if self.listening == mic.index and self.out_stream is not None:
                y = self._resample(x, mic.rate, self.out_rate)
                if self.gain != 1.0:
                    y = np.clip(y * self.gain, -1.0, 1.0)
                self._push(y)
        return cb

    @staticmethod
    def _resample(x, src_rate, dst_rate):
        if src_rate == dst_rate or len(x) == 0:
            return x.astype(np.float32, copy=True)
        n = max(1, int(round(len(x) * dst_rate / src_rate)))
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)

    def _push(self, chunk):
        with self.buf_lock:
            self.chunks.append(chunk)
            self.buffered += len(chunk)
            cap = int(self.out_rate * MAX_BUFFER_SECONDS)
            while self.buffered > cap and self.chunks:
                self.buffered -= len(self.chunks.popleft())

    def _out_cb(self, outdata, frames, t, status):
        out = np.zeros(frames, dtype=np.float32)
        pos = 0
        with self.buf_lock:
            while pos < frames and self.chunks:
                c = self.chunks[0]
                n = min(len(c), frames - pos)
                out[pos:pos + n] = c[:n]
                if n == len(c):
                    self.chunks.popleft()
                else:
                    self.chunks[0] = c[n:]
                self.buffered -= n
                pos += n
        outdata[:] = out[:, None]

    # ---- controls
    def toggle(self, mic_id: int):
        with self.lock:
            self.listening = None if self.listening == mic_id else mic_id
            with self.buf_lock:
                self.chunks.clear()
                self.buffered = 0
            name = next((m.name for m in self.mics if m.index == self.listening), None)
            print(f"Listening to: {name}" if name else "Listening stopped")

    def state(self) -> dict:
        with self.lock:
            return {
                "mics": [m.to_dict(self.listening) for m in self.mics],
                "output": self.out_name,
                "output_id": self.out_index,
                "outputs": self.outputs,
                "message": self.message,
            }

    def close(self):
        with self.lock:
            self._stop_all()


class DemoMonitor:
    """Fake mics for testing the screen without hardware."""

    def __init__(self):
        self.listening = None
        self.names = ["AirPods Pro", "Jabra Talk 30", "JBL Tune 510BT", "Sony WH-1000XM5", "Rode Wireless GO"]
        self.mics = [Mic(i, n, 16000, 1, True) for i, n in enumerate(self.names[:4])]
        self.phase = [random.random() * 6 for _ in self.mics]

    def scan(self):
        print("Demo mode: 4 fake Bluetooth mics")

    def toggle(self, mic_id):
        self.listening = None if self.listening == mic_id else mic_id

    def state(self):
        t = time.monotonic()
        for i, m in enumerate(self.mics):
            talking = math.sin(t * 0.7 + self.phase[i]) > 0.1
            rms = (0.04 + 0.06 * abs(math.sin(t * 9 + i))) if talking else 0.003
            m.level = rms if rms > m.level else m.level * 0.85 + rms * 0.15
            if rms > ACTIVE_THRESHOLD:
                m.last_loud = t
        return {"mics": [m.to_dict(self.listening) for m in self.mics], "output": "Demo output",
                "output_id": 0, "outputs": [{"id": 0, "name": "Demo output"}], "message": ""}

    def close(self):
        pass


# --------------------------------------------------------------------------- Web screen

PAGE = (Path(__file__).with_name("screen.html")).read_text(encoding="utf-8")


def make_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/state":
                self._send(200, json.dumps(monitor.state()).encode(), "application/json")
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(f"data: {json.dumps(monitor.state())}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path.startswith("/listen/"):
                try:
                    monitor.toggle(int(self.path.rsplit("/", 1)[1]))
                except ValueError:
                    return self._send(400, b"bad id", "text/plain")
                self._send(200, b"ok", "text/plain")
            elif self.path.startswith("/output/"):
                try:
                    monitor.set_output(int(self.path.rsplit("/", 1)[1]))
                except (ValueError, AttributeError):
                    return self._send(400, b"bad id", "text/plain")
                self._send(200, b"ok", "text/plain")
            elif self.path == "/rescan":
                monitor.scan()
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Bluetooth Mic Monitor")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--list", action="store_true", help="print the audio devices Python sees and exit")
    ap.add_argument("--demo", action="store_true", help="fake mics, no audio hardware needed")
    ap.add_argument("--bluetooth-only", action="store_true", help="hide every mic that isn't Bluetooth")
    ap.add_argument("--include-builtin", action="store_true", help="also show the MacBook's built-in mic")
    ap.add_argument("--output", help="play through this output (part of its name), e.g. --output AirPods. "
                                     "Default: the laptop speakers")
    ap.add_argument("--gain", type=float, default=2.0, help="playback boost for quiet Bluetooth mics (default 2.0)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the screen automatically")
    args = ap.parse_args()

    if args.demo:
        monitor = DemoMonitor()
    else:
        load_audio_libs()
        if args.list:
            print(sd.query_devices())
            bt = connected_bluetooth_names()
            print("\nConnected Bluetooth devices:", ", ".join(bt) if bt else "(none detected)")
            return
        monitor = Monitor(args.bluetooth_only, args.include_builtin, args.output, args.gain)

    monitor.scan()
    if hasattr(monitor, "watchdog"):
        threading.Thread(target=monitor.watchdog, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(monitor))
    server.daemon_threads = True
    url = f"http://localhost:{args.port}"
    print(f"\nMic Monitor screen: {url}   (Ctrl+C to quit)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye")
    finally:
        monitor.close()
        server.server_close()


if __name__ == "__main__":
    main()

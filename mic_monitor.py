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


def call_with_timeout(fn, timeout: float, what: str):
    """Run a PortAudio/Core Audio call on a helper thread so a stuck call can't freeze the app.

    macOS can block forever when a Bluetooth device changes mode or refuses a second
    headset mic. If that happens we give up on the call and report it instead of hanging.
    """
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"macOS audio did not respond while {what} (waited {timeout:g}s)")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class Monitor:
    OPEN_TIMEOUT = 4.0
    CLOSE_TIMEOUT = 2.0

    def __init__(self, bluetooth_only: bool, include_builtin: bool, output: str | None, gain: float):
        self.bluetooth_only = bluetooth_only
        self.include_builtin = include_builtin
        self.output_pref = output   # name substring from --output, or None for laptop speakers
        self.gain = gain
        self.out_index: int | None = None
        self.outputs: list[dict] = []
        # Only one scan / device change at a time. Nothing the screen calls waits on this,
        # so the screen keeps updating even if macOS is slow to answer.
        self.audio_lock = threading.Lock()
        self.mics: list[Mic] = []
        self.listening: int | None = None
        self.out_stream = None
        self.out_rate = 48000.0
        self.out_name = ""
        self.chunks: collections.deque = collections.deque()
        self.buffered = 0
        self.buf_lock = threading.Lock()
        self.message = ""
        self.scanning = False
        self.last_out_retry = 0.0

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
    def scan_async(self):
        if self.scanning:
            return
        threading.Thread(target=self.scan, daemon=True).start()

    def scan(self):
        if not self.audio_lock.acquire(blocking=False):
            return
        self.scanning = True
        self.message = "Scanning devices…"
        try:
            self._stop_all()
            # PortAudio caches the device list; re-initialise it to see newly connected devices.
            try:
                call_with_timeout(lambda: (sd._terminate(), sd._initialize()), 6, "re-reading the device list")
            except TimeoutError as e:
                self.mics = []
                self.message = f"{e}. Quit (Ctrl+C) and start the app again."
                print(self.message)
                return
            except Exception:
                pass
            mics = self.find_inputs()
            # Open the mics one by one. A mic macOS refuses (or that hangs) gets an error on its
            # card; the others keep working.
            for mic in mics:
                self._open_input(mic)
                time.sleep(0.3)  # give Core Audio a moment between Bluetooth mode switches
            self.mics = mics
            if self.listening is not None and not any(m.index == self.listening and not m.error for m in mics):
                self.listening = None
            # Open the output last: opening a Bluetooth mic can move the system output to it.
            time.sleep(0.5)
            self.outputs = self.list_outputs()
            self._open_output(self.pick_output())
            ok = [m for m in mics if m.error is None]
            bt_ok = [m for m in ok if m.bluetooth]
            bt_bad = [m for m in mics if m.bluetooth and m.error]
            if not mics:
                self.message = "No mics found"
            elif bt_bad and bt_ok:
                self.message = ("macOS is only running some of the Bluetooth mics. "
                                "Macs usually allow one Bluetooth headset mic at a time.")
            else:
                self.message = ""
            print(f"Monitoring {len(ok)} of {len(mics)} mic(s):")
            for m in mics:
                tag = "Bluetooth" if m.bluetooth else "other"
                print(f"  [{m.index}] {m.name} ({tag}, {int(m.rate)} Hz)" + (f"  ERROR: {m.error}" if m.error else ""))
        finally:
            self.scanning = False
            self.audio_lock.release()

    def _stop_stream(self, stream, what):
        if stream is None:
            return
        try:
            # abort() doesn't wait for buffers to drain, so it can't block on a dead device.
            call_with_timeout(lambda: (stream.abort(), stream.close()), self.CLOSE_TIMEOUT, what)
        except Exception as e:
            print(f"Ignoring: {e}")

    def _stop_all(self):
        mics, self.mics = self.mics, []
        for m in mics:
            s, m.stream = m.stream, None
            self._stop_stream(s, f"closing {m.name}")
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
        s, self.out_stream = self.out_stream, None
        self._stop_stream(s, "closing the output")

    def _open_output(self, index: int | None):
        self._close_output()
        try:
            dev = sd.query_devices(index) if index is not None else sd.query_devices(kind="output")
            self.out_index = index
            rate = float(dev["default_samplerate"])
            ch = min(2, int(dev["max_output_channels"])) or 1

            def make():
                s = sd.OutputStream(device=index, samplerate=rate, channels=ch, dtype="float32",
                                    callback=self._out_cb, latency="low")
                s.start()
                return s

            self.out_rate = rate
            self.out_stream = call_with_timeout(make, self.OPEN_TIMEOUT, f"opening {dev['name']}")
            self.out_name = dev["name"]
        except Exception as e:
            self.out_stream = None
            self.out_name = f"unavailable ({str(e).splitlines()[0][:80]})"
        print(f"Output (what you hear): {self.out_name}")

    def set_output(self, index: int):
        def run():
            with self.audio_lock:
                self.output_pref = None
                self._open_output(index)
        threading.Thread(target=run, daemon=True).start()

    def watchdog(self):
        """Notice streams macOS killed (it can when Bluetooth devices change mode)."""
        while True:
            time.sleep(1.0)
            if not self.audio_lock.acquire(blocking=False):
                continue  # a scan or output change is running
            try:
                for m in self.mics:
                    if m.stream is not None and m.error is None and not m.stream.active:
                        m.error = "macOS stopped this mic"
                        print(f"{m.name}: {m.error}")
                        if self.listening == m.index:
                            self.listening = None
                s = self.out_stream
                now = time.monotonic()
                if (s is None or not s.active) and now - self.last_out_retry > 3:
                    self.last_out_retry = now
                    print("Output stream stopped; reopening it.")
                    self._open_output(self.out_index)
            except Exception as e:
                print(f"Watchdog: {e}")
            finally:
                self.audio_lock.release()

    def _open_input(self, mic: Mic):
        def make():
            s = sd.InputStream(device=mic.index, channels=min(mic.channels, 2), samplerate=mic.rate,
                               dtype="float32", callback=self._make_in_cb(mic), latency="low")
            s.start()
            return s
        try:
            mic.stream = call_with_timeout(make, self.OPEN_TIMEOUT, f"opening {mic.name}")
        except TimeoutError:
            mic.stream = None
            mic.error = "macOS didn't start this mic (likely the one-Bluetooth-mic limit)"
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
        mic = next((m for m in self.mics if m.index == mic_id), None)
        if mic is None or mic.error:
            return
        self.listening = None if self.listening == mic_id else mic_id
        with self.buf_lock:
            self.chunks.clear()
            self.buffered = 0
        print(f"Listening to: {mic.name}" if self.listening is not None else "Listening stopped")

    def state(self) -> dict:
        # No locks here: the screen must keep updating even while macOS is busy.
        mics = list(self.mics)
        return {
            "mics": [m.to_dict(self.listening) for m in mics],
            "output": self.out_name,
            "output_id": self.out_index,
            "outputs": list(self.outputs),
            "message": self.message,
            "scanning": self.scanning,
        }

    def close(self):
        try:
            call_with_timeout(self._stop_all, 4, "shutting down")
        except Exception:
            pass


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
                getattr(monitor, "scan_async", monitor.scan)()
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

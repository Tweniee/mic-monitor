# Mic Monitor

Shows every connected Bluetooth mic (up to 5), lights up the ones picking up sound,
and lets you click one to hear only that mic. Audio is read straight from macOS
with Python (`sounddevice`), so it doesn't depend on browser mic permissions.
The window that opens at http://localhost:8765 is just the screen.

## Run (macOS)

```bash
git clone <this repo> && cd mic-monitor
./run.sh                 # first run installs numpy + sounddevice into .venv
```

First time only: macOS asks whether Terminal may use the microphone. Allow it.
If you missed it: System Settings > Privacy & Security > Microphone > turn on Terminal
(or iTerm / VS Code), then quit and reopen that app.

## Useful options

| Command | What it does |
|---|---|
| `./run.sh --list` | Print every audio device Python sees and the connected Bluetooth devices |
| `./run.sh --bluetooth-only` | Hide every mic that isn't Bluetooth |
| `./run.sh --include-builtin` | Also show the MacBook's built-in mic (hidden by default) |
| `./run.sh --demo` | Fake mics, for testing the screen or recording B-roll |

Connected a new device while it's running? Press **Rescan devices**.

## If a mic doesn't show up

1. Run `./run.sh --list`. If the mic isn't in that list, macOS isn't exposing it as an input:
   pick it once in System Settings > Sound > Input, then rescan.
2. If it's listed but its card says "Could not open this mic", macOS likely can't run
   that many Bluetooth mics at once. Disconnect one and rescan.
3. Cards show up but never light up: Terminal doesn't have microphone permission (see above).

Listen on headphones. Bluetooth mics switch to "call quality" audio while in use; that's normal.

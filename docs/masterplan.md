# Masterplan: Bluetooth Mic Monitor (Reel Side Project)

## 1. Overview and Objective
A tiny single-screen app on a MacBook that shows every connected Bluetooth microphone (max 5), indicates which ones are picking up sound right now, and lets you click one to listen to only that mic.

Purpose: a visual demo for a reel. Not a production product. Target build time: 1-2 hours.

## 2. Target Audience
Just you, and the viewers of your reel. Single user, single laptop, no accounts.

## 3. Core Features
1. **Device list**: shows up to 5 audio input devices (Bluetooth mics) connected to the Mac, each as a card with its name.
2. **Live activity indicator**: each card shows an icon (e.g. a pulsing speaker or sound-wave icon) plus a small volume bar when that mic is capturing audio above a quiet threshold.
3. **Click to listen (solo)**: clicking a card plays only that mic through your headphones. Clicking again stops it. Only one device can be "listened to" at a time.
4. **Silent monitoring for the rest**: the other mics are still analysed for activity (icon lights up) but are not played out loud.

Everything else (recording, saving, settings, naming devices) is intentionally out of scope.

## 4. Recommended Approach (high level)
**A single local web page that runs in Chrome or Safari on the Mac.**

| Option | Pros | Cons |
|---|---|---|
| **Local web page (recommended)** | Fastest to build, built-in access to all mics, easy to make look good on screen for the reel | Browser asks for mic permission; browser may limit how many mics it opens at once |
| Native Mac app (Swift) | Most control over audio | Much slower to build, overkill for a reel |
| Python desktop app | Quick to script | Uglier UI, more setup, worse for visuals |

Why web page: it gives you everything needed (list mics, open several at once, measure loudness, play one) with the least effort, and it looks good on camera.

Supporting pieces, all built into the browser: device listing, live audio capture, loudness measuring, audio playback. No server, no database, no third-party services.

## 5. Conceptual Data Model
No storage at all. Everything lives in memory while the page is open:
- **Device**: name, id, current loudness level, "is active" flag
- **Selection**: which single device (if any) is currently being listened to

## 6. UI Principles
- One screen, dark background, big readable cards (good for screen recording).
- Each card: device name, activity icon (lit when sound is detected, dim when silent), volume bar, and a clear "listening" highlight on the selected card.
- Empty state: a simple "No Bluetooth mics found, connect a device" message.
- Show a headphone reminder to avoid feedback.

## 7. Security and Privacy
- Mic access is granted by you through the browser permission prompt.
- Audio never leaves the laptop and is never recorded or saved.
- Only meant for your own devices or people who know they are being heard (worth saying in the reel).

## 8. Development Phases (about 1-2 hours total)
1. **Prove it works (15 min)**: connect your real Bluetooth devices, see how many mics the Mac and browser can handle at once. This is the biggest risk, so test it first.
2. **Device list + activity icons (30 min)**: show cards and live loudness.
3. **Click to listen (20 min)**: play one mic at a time.
4. **Polish for the reel (20 min)**: look, animations, empty state, test screen recording.

## 9. Potential Challenges and Solutions
| Challenge | Solution |
|---|---|
| Mac may not keep 5 Bluetooth mics active at once (Bluetooth bandwidth limit) | Test early; demo with the number that works reliably (likely 2-3), or mix in a couple of wired/USB mics to fill the screen |
| Bluetooth mics give low-quality "phone call" audio | Expected and fine for this demo; mention it if asked |
| Feedback squeal if you listen on laptop speakers | Always listen on headphones |
| Browser may hide device names until mic permission is granted | Grant permission on first load; the list then fills in |
| Some Bluetooth devices only expose a mic when the Mac selects them as input | Connect them first; if one doesn't appear, toggle it in System Settings > Sound > Input |

## 10. Future Ideas (optional, only if the reel does well)
- Name/rename devices and assign colours
- Listen to several mics at once with a mixer
- Record a clip from a selected mic
- Turn it into a proper Mac app

# UNO Q Internet Radio - Linux side (Qualcomm QRB2210, runs in Arduino App Lab)
#
# Responsibilities
#   * load the station list (stations.json)
#   * resolve stream URLs (direct URL first, Radio Browser directory as fallback)
#   * decode the stream with PyAV and play it on a USB sound card through ALSA
#   * react to button events sent by the STM32 sketch over the Router Bridge
#   * draw station number, play state and volume on the 13x8 LED matrix
#
# SPDX-License-Identifier: MIT

import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

import av
import numpy as np
import alsaaudio
from arduino.app_utils import App, Bridge

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "stations.json")
STATE_FILE = os.path.join(HERE, "state.json")
USER_AGENT = "UNOQ-InternetRadio/1.0"

# Button event codes - must match the #defines in sketch.ino
EV_PREV, EV_PLAY, EV_NEXT, EV_VOL_DOWN, EV_VOL_UP, EV_MUTE = 1, 2, 3, 4, 5, 6

# Player states
IDLE, CONNECTING, PLAYING, ERROR = "idle", "connecting", "playing", "error"

RB_SERVERS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]


# --------------------------------------------------------------------------
# Configuration and persisted state
# --------------------------------------------------------------------------
def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("stations"):
        raise RuntimeError("stations.json contains no stations")
    return cfg


def load_state(default_volume, n_stations):
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return (int(s.get("station", 0)) % n_stations,
                max(0, min(100, int(s.get("volume", default_volume)))))
    except Exception:
        return 0, default_volume


def save_state(station, volume):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"station": station, "volume": volume}, f)
    except Exception as e:
        print(f"[state] could not save: {e}")


# --------------------------------------------------------------------------
# Station URL resolution
# --------------------------------------------------------------------------
_rb_cache = {}


def radio_browser_lookup(name, countrycode=None):
    """Return a working stream URL from the Radio Browser directory, or None."""
    key = (name, countrycode)
    if key in _rb_cache:
        return _rb_cache[key]
    params = {"name": name, "hidebroken": "true", "order": "clickcount",
              "reverse": "true", "limit": "10"}
    if countrycode:
        params["countrycode"] = countrycode
    query = urllib.parse.urlencode(params)
    for server in RB_SERVERS:
        try:
            req = urllib.request.Request(
                f"{server}/json/stations/search?{query}",
                headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as r:
                results = json.load(r)
            for st in results:
                url = st.get("url_resolved") or st.get("url")
                codec = (st.get("codec") or "").upper()
                if url and codec in ("MP3", "AAC", "AAC+", "OGG", "UNKNOWN", ""):
                    print(f"[rb] '{name}' -> {st.get('name')} ({codec}) {url}")
                    _rb_cache[key] = url
                    return url
            return None
        except Exception as e:
            print(f"[rb] {server} failed: {e}")
    return None


def candidate_urls(station):
    """Yield URLs to try for a station: direct URL, then directory lookup."""
    if station.get("url"):
        yield station["url"]
    if station.get("rb_search"):
        url = radio_browser_lookup(station["rb_search"], station.get("rb_countrycode"))
        if url and url != station.get("url"):
            yield url


# --------------------------------------------------------------------------
# ALSA output
# --------------------------------------------------------------------------
def find_usb_device():
    """Return an ALSA device string for the first USB sound card."""
    for idx in alsaaudio.card_indexes():
        short, long_name = alsaaudio.card_name(idx)
        if "usb" in long_name.lower() or "usb" in short.lower():
            print(f"[audio] using card {idx}: {long_name}")
            return f"plughw:{idx},0"
    print("[audio] no USB card found, falling back to 'default'")
    return "default"


class AlsaOutput:
    PERIOD = 1024  # frames per write

    def __init__(self, device, rate):
        self.rate = rate
        self.pcm = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK,
                                 mode=alsaaudio.PCM_NORMAL,
                                 device=device, channels=2, rate=rate,
                                 format=alsaaudio.PCM_FORMAT_S16_LE,
                                 periodsize=self.PERIOD)
        self.period_bytes = self.PERIOD * 2 * 2
        self.buf = bytearray()

    def write(self, data: bytes):
        self.buf += data
        while len(self.buf) >= self.period_bytes:
            self.pcm.write(bytes(self.buf[:self.period_bytes]))
            del self.buf[:self.period_bytes]

    def flush_silence(self):
        self.buf.clear()

    def close(self):
        try:
            self.pcm.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Stream player (runs in its own thread)
# --------------------------------------------------------------------------
class Player:
    def __init__(self, output: AlsaOutput):
        self.out = output
        self.state = IDLE
        self.volume = 60
        self.muted = False
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def gain(self):
        if self.muted:
            return 0.0
        v = self.volume / 100.0
        return v * v  # perceptual (roughly logarithmic) curve

    def play(self, station):
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(station, self._stop),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._stop.set()
                self._thread.join(timeout=5)
            self._thread = None
            self.out.flush_silence()
            self.state = IDLE

    def _run(self, station, stop_evt):
        retries = 0
        while not stop_evt.is_set() and retries < 3:
            self.state = CONNECTING
            played_something = False
            for url in candidate_urls(station):
                if stop_evt.is_set():
                    return
                try:
                    played_something = self._stream(url, stop_evt)
                    if played_something or stop_evt.is_set():
                        break
                except Exception as e:
                    print(f"[player] {station['name']}: {url} failed: {e}")
            if stop_evt.is_set():
                return
            retries += 1
            if retries < 3:
                print(f"[player] reconnecting ({retries}/3)...")
                time.sleep(2)
        if not stop_evt.is_set():
            self.state = ERROR

    def _stream(self, url, stop_evt):
        print(f"[player] opening {url}")
        options = {"user_agent": USER_AGENT, "reconnect": "1",
                   "reconnect_streamed": "1", "reconnect_delay_max": "5"}
        container = av.open(url, options=options, timeout=(10.0, 15.0))
        played = False
        try:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="stereo",
                                          rate=self.out.rate)
            title = container.metadata.get("icy-name") or ""
            if title:
                print(f"[player] station says: {title}")
            for packet in container.demux(stream):
                if stop_evt.is_set():
                    break
                for frame in packet.decode():
                    for out_frame in resampler.resample(frame):
                        pcm = out_frame.to_ndarray().reshape(-1)  # interleaved int16
                        g = self.gain()
                        if g < 0.999:
                            pcm = (pcm.astype(np.float32) * g).astype(np.int16)
                        self.out.write(pcm.tobytes())
                        if not played:
                            played = True
                            self.state = PLAYING
                    if stop_evt.is_set():
                        break
        finally:
            container.close()
        return played


# --------------------------------------------------------------------------
# LED matrix rendering (13 columns x 8 rows)
# --------------------------------------------------------------------------
COLS, ROWS = 13, 8

DIGITS = {  # 3x5 font, each row is 3 bits (MSB = left column)
    "0": [7, 5, 5, 5, 7], "1": [2, 6, 2, 2, 7], "2": [7, 1, 7, 4, 7],
    "3": [7, 1, 7, 1, 7], "4": [5, 5, 7, 1, 1], "5": [7, 4, 7, 1, 7],
    "6": [7, 4, 7, 5, 7], "7": [7, 1, 2, 2, 2], "8": [7, 5, 7, 5, 7],
    "9": [7, 5, 7, 1, 7],
}
ICONS = {  # 4x5 icons drawn at columns 9-12
    PLAYING: [8, 12, 14, 12, 8],      # play triangle
    IDLE:    [10, 10, 10, 10, 10],    # pause bars
    ERROR:   [9, 6, 6, 6, 9],         # X
}


def render(station_idx, state, volume, muted, tick):
    grid = [[0] * COLS for _ in range(ROWS)]

    def put(x, y):
        if 0 <= x < COLS and 0 <= y < ROWS:
            grid[y][x] = 1

    # Station number 01..99 at rows 1-5, columns 0-2 and 4-6
    label = f"{(station_idx + 1) % 100:02d}"
    for d, ch in enumerate(label):
        for r, bits in enumerate(DIGITS[ch]):
            for c in range(3):
                if bits & (4 >> c):
                    put(d * 4 + c, r + 1)

    # State icon at columns 9-12
    if state == CONNECTING:
        for i in range(tick % 4 + 1):            # growing progress dots
            put(9 + i, 3)
    else:
        for r, bits in enumerate(ICONS.get(state, ICONS[IDLE])):
            for c in range(4):
                if bits & (8 >> c):
                    put(9 + c, r + 1)

    # Volume bar on the bottom row (blinks when muted)
    if not (muted and tick % 2):
        lit = round(volume / 100 * COLS)
        for x in range(lit):
            put(x, 7)

    # Pack row-major, MSB-first into four 32-bit words (104 bits + padding)
    bits = [grid[y][x] for y in range(ROWS) for x in range(COLS)] + [0] * 24
    words = []
    for w in range(4):
        v = 0
        for b in bits[w * 32:(w + 1) * 32]:
            v = (v << 1) | b
        words.append(v)
    return "".join(f"{w:08x}" for w in words)


# --------------------------------------------------------------------------
# Application glue
# --------------------------------------------------------------------------
cfg = load_config()
stations = cfg["stations"]
current, start_volume = load_state(int(cfg.get("default_volume", 60)), len(stations))

device = cfg.get("audio_device", "auto")
output = AlsaOutput(find_usb_device() if device == "auto" else device,
                    int(cfg.get("sample_rate", 48000)))
player = Player(output)
player.volume = start_volume

events = queue.Queue()
playing_wanted = True
last_frame = None
last_leds = None
tick = 0
last_tick_time = 0.0


def on_button(code):
    """Called by the Router Bridge when the sketch sends Bridge.notify()."""
    events.put(int(code))


Bridge.provide("button_event", on_button)


def tune(idx):
    global current
    current = idx % len(stations)
    print(f"[radio] station {current + 1}: {stations[current]['name']}")
    save_state(current, player.volume)
    if playing_wanted:
        player.play(stations[current])


def handle(ev):
    global playing_wanted
    if ev == EV_NEXT:
        tune(current + 1)
    elif ev == EV_PREV:
        tune(current - 1)
    elif ev == EV_PLAY:
        playing_wanted = not playing_wanted
        if playing_wanted:
            player.play(stations[current])
        else:
            player.stop()
    elif ev == EV_VOL_UP:
        player.volume = min(100, player.volume + 5)
        player.muted = False
        save_state(current, player.volume)
    elif ev == EV_VOL_DOWN:
        player.volume = max(0, player.volume - 5)
        save_state(current, player.volume)
    elif ev == EV_MUTE:
        player.muted = not player.muted


def update_outputs():
    """Push matrix frame and button LEDs to the sketch when they change."""
    global last_frame, last_leds
    frame = render(current, player.state, player.volume, player.muted, tick)
    # LED A = previous, B = playing, C = next; A/C light while connecting
    leds = (0b010 if player.state == PLAYING else 0) | \
           (0b101 if player.state == CONNECTING and tick % 2 else 0)
    try:
        if frame != last_frame:
            Bridge.call("show_frame", frame)
            last_frame = frame
        if leds != last_leds:
            Bridge.call("set_leds", leds)
            last_leds = leds
    except Exception as e:
        # Sketch not ready yet (e.g. still flashing) - try again next loop
        print(f"[bridge] {e}")
        last_frame = last_leds = None
        time.sleep(0.5)


def loop():
    global tick, last_tick_time
    try:
        while True:
            handle(events.get_nowait())
    except queue.Empty:
        pass
    now = time.monotonic()
    if now - last_tick_time > 0.4:
        tick += 1
        last_tick_time = now
    update_outputs()
    time.sleep(0.05)


print(f"[radio] {len(stations)} stations loaded, starting at #{current + 1}")
tune(current)
App.run(user_loop=loop)

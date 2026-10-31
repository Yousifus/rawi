"""The ear: always-on listening with a voice-activity gate that learns the room.

A latch, not a hold. One key down and the microphone stays open; voice activity decides where a
sentence begins and ends, and each finished sentence is handed to `on_segment` (the daemon
transcribes it and puts it on the clipboard). Nothing is pasted into a window the speaker did not
choose.

The detector is pure numpy and has no idea where audio comes from, so tests drive it with
synthetic blocks:

    ear = Ear(get_cfg=lambda: cfg["listen"], on_segment=print, start_worker=False)
    for block in blocks: ear.feed(block)
    ear.q.get_nowait()   # the finished sentence, a float32 array
"""
from __future__ import annotations

import queue
import threading
from typing import Callable

import numpy as np

SR = 16000              # samples per second of every block fed in

# --- gate tuning (unchanged from the original daemon; every number was tuned by ear) ---
START_FACTOR = 2.4      # a block this many times louder than the floor counts toward "speaking"
START_ABS = 0.0016      # ...and it must also clear this absolute level
START_SECONDS = 0.22    # 0.22 s of real voice, not a click, opens a sentence
KEEP_FACTOR = 1.25      # once talking, a block this much above the floor is still voice
KEEP_ABS = 0.0009
FLOOR_START = 0.0012    # the noise floor before anything has been heard
FLOOR_MIN = 0.0004      # the floor never decays below this
PRE_BLOCKS = 14         # blocks kept from before the gate opened (the half-word that woke us)
QUIET_WINDOW = 133      # blocks of recent levels kept while talking (~2 s)
QUIET_MIN_BLOCKS = 100  # ...and how many are needed before they may raise the floor
MAX_WAITING = 3         # finished sentences queued for the model before the oldest is dropped


class Ear:
    """Voice-activity detector + a worker thread that hands finished sentences on.

    get_cfg      returns the `listen` config section (silence_ms, min_s, max_s); read per block
    on_segment   called on the worker thread with each finished sentence (float32, mono, SR)
    start_worker start the worker thread now (tests pass False and read `q` directly)

    Counters (`emits`, `short`, `dropped`, `done`, `empty`, `errors`) and levels (`lvl`, `peak`,
    `floor`, `talking`) are read by the daemon's status reply and shown in the hub.
    """

    def __init__(self, get_cfg: Callable[[], dict], on_segment: Callable[[np.ndarray], None],
                 start_worker: bool = True):
        self.get_cfg = get_cfg
        self.on_segment = on_segment
        self.on = False                     # the latch: only an open ear passes sentences on
        self.q: queue.Queue = queue.Queue()
        self.buf: list[np.ndarray] = []
        self.voiced = 0.0                   # seconds of voice in the current sentence
        self.silent = 0.0                   # seconds of trailing silence in the current sentence
        self.total = 0.0                    # seconds in the current sentence
        self.talking = False
        self.floor = FLOOR_START
        self.lvl = 0.0                      # RMS of the latest block
        self.peak = 0.0                     # slowly decaying peak RMS, for the hub's meter
        self.dropped = 0
        self.emits = 0
        self.short = 0
        self.done = 0
        self.empty = 0
        self.errors = 0
        self.hot = 0.0                      # seconds of loud blocks while waiting to start
        self.pre: list[np.ndarray] = []
        self.qwin: list[float] = []
        self.PRE = PRE_BLOCKS
        if start_worker:
            threading.Thread(target=self._loop, daemon=True).start()

    def limits(self) -> dict:
        """The listen config as seconds: {"silence", "min", "max"}."""
        c = self.get_cfg() or {}
        return {"silence": float(c.get("silence_ms", 700)) / 1000.0,
                "min": float(c.get("min_s", 0.45)),
                "max": float(c.get("max_s", 25.0))}

    def reset(self) -> None:
        """Forget a half-heard sentence (used when the ear is closed)."""
        self.talking = False
        self.buf = []

    def feed(self, block: np.ndarray) -> None:
        """Called from the audio callback: light numpy only, never transcription.

        Two thresholds, not one: a sentence starts when you clearly speak and ends when
        you are clearly quiet, so room noise cannot hold the ear open forever.
        """
        try:
            c = self.limits()
            a = block.reshape(-1)
            if not a.size:
                return
            rms = float(np.sqrt(np.mean(np.square(a))))
            self.lvl = rms
            self.peak = max(self.peak * 0.995, rms)
            dtb = a.size / float(SR)
            if not self.talking:
                self.pre.append(a.copy())
                del self.pre[:-self.PRE]
                if rms > max(self.floor * START_FACTOR, START_ABS):
                    self.hot += dtb
                else:
                    self.hot = 0.0
                    # the floor learns only from what is NOT voice — a fan raises it,
                    # speech never does, so speaking can never close the gate
                    self.floor = ((0.80 * self.floor + 0.20 * rms) if rms > self.floor
                                  else max(0.97 * self.floor, FLOOR_MIN))
                if self.hot >= START_SECONDS:
                    self.talking = True
                    self.buf = list(self.pre)              # keep the half-word that woke us
                    self.voiced = self.silent = self.total = self.hot
                    self.hot = 0.0
            if self.talking:
                # a room that gets loud mid-sentence must not hold the ear open for 25 s:
                # the quietest moment of the last two seconds becomes the floor.
                self.qwin.append(rms)
                del self.qwin[:-QUIET_WINDOW]
                if len(self.qwin) >= QUIET_MIN_BLOCKS:
                    self.floor = max(self.floor, 0.85 * min(self.qwin))
                self.buf.append(a.copy())
                self.total += dtb
                if rms > max(self.floor * KEEP_FACTOR, KEEP_ABS):
                    self.voiced += dtb
                    self.silent = 0.0
                else:
                    self.silent += dtb
                if self.silent >= c["silence"] or self.total >= c["max"]:
                    self._finish(c["min"])
        except Exception as e:
            self.errors += 1
            if self.errors <= 3:
                print(f"[rawi] ear feed error {type(e).__name__}: {e}", flush=True)

    def _finish(self, min_voiced: float) -> None:
        """Close the current sentence: queue it if it held enough voice, else count it as short."""
        seg = np.concatenate(self.buf) if self.buf else None
        voiced = self.voiced
        self.talking = False
        self.buf = []
        self.pre = []
        self.qwin = []
        self.silent = 0.0
        self.hot = 0.0
        if seg is not None and voiced >= min_voiced:
            if self.q.qsize() >= MAX_WAITING:          # the speaker is faster than the model
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass
            self.emits += 1
            self.q.put(seg)
        else:
            self.short += 1

    def _loop(self) -> None:
        """Worker: hand each queued sentence to `on_segment` while the ear is open."""
        while True:
            seg = self.q.get()
            try:
                if not self.on:
                    continue
                self.done += 1
                self.on_segment(seg)
            except Exception as e:
                print(f"[rawi] ear error {type(e).__name__}: {e}", flush=True)

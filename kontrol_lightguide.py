#!/usr/bin/env python3
"""Light Guide bridge for Native Instruments Kontrol S-Series MK3 keyboards on Linux.

Creates a virtual ALSA MIDI port. Notes arriving there (from PianoGlow, Synthesia
or any other app) light up the matching keys. The keyboard is driven through HID
output reports on interface 2 (/dev/hidrawN), no NI software involved:

  A0 00 00    enter LED mode (key MIDI pauses for a moment)
  82 + n      one colour byte per key, index 0 = leftmost key
  A0 01 00    leave LED mode, MIDI keeps working, LEDs stay lit

Protocol found by ca9 on an S88 MK3 (KompleteSynthesia discussion #29),
confirmed on an S61 MK3.
"""

from __future__ import annotations

import argparse
import glob
import os
import queue
import sys
import time

VID = 0x17CC
MODELS = {
    0x2100: "Kontrol S49 MK3",
    0x2110: "Kontrol S61 MK3",
    0x2120: "Kontrol S88 MK3",
}
TESTED = {0x2110}
HID_INTERFACE = "1.2"
PORT_NAME = "Kontrol Light Guide"

OFF = 0x00
COLORS = {
    "red": 0x06,
    "orange": 0x0C,
    "yellow": 0x12,
    "green": 0x1E,
    "blue": 0x2C,
    "white": 0x7F,
}


# --------------------------------------------------------------------------
# HID
# --------------------------------------------------------------------------


def output_report_sizes(descriptor: bytes) -> dict[int, int]:
    """Parse a HID report descriptor, return {report id: payload bytes} for outputs."""
    sizes: dict[int, int] = {}
    i, rid, rsize, rcount = 0, 0, 0, 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item, never used for this
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        tag = prefix & 0xFC
        value = int.from_bytes(descriptor[i + 1 : i + 1 + size], "little")
        i += 1 + size
        if tag == 0x84:  # Report ID
            rid = value
        elif tag == 0x74:  # Report Size (bits)
            rsize = value
        elif tag == 0x94:  # Report Count
            rcount = value
        elif tag == 0x90:  # Output
            sizes[rid] = sizes.get(rid, 0) + rsize * rcount
    return {r: bits // 8 for r, bits in sizes.items()}


def find_hidraw() -> tuple[str, int]:
    """Find the hidraw node of HID interface 2. Returns (path, product id)."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                uevent = f.read()
        except OSError:
            continue
        pid = next((p for p in MODELS if f"HID_ID=0003:{VID:08X}:{p:08X}" in uevent), None)
        if pid is None:
            continue
        # device -> HID node (0003:17CC:2110.000N), its parent is the USB interface (5-3:1.2)
        usb_iface = os.path.basename(os.path.dirname(os.path.realpath(os.path.join(node, "device"))))
        if usb_iface.rsplit(":", 1)[-1] == HID_INTERFACE:
            return "/dev/" + os.path.basename(node), pid
    raise FileNotFoundError("no Kontrol S-Series MK3 found (HID interface 2)")


class LightGuide:
    def __init__(self, path: str | None = None):
        if path:
            self.path, self.pid = path, None
        else:
            self.path, self.pid = find_hidraw()
        sysfs = f"/sys/class/hidraw/{os.path.basename(self.path)}/device/report_descriptor"
        with open(sysfs, "rb") as f:
            sizes = output_report_sizes(f.read())
        if 0x82 not in sizes or 0xA0 not in sizes:
            raise RuntimeError(f"{self.path}: no light guide reports in HID descriptor")
        self.nkeys = sizes[0x82]
        self.len_a0 = sizes[0xA0]
        self.model = MODELS.get(self.pid, "unknown model")
        self.fd = os.open(self.path, os.O_WRONLY)

    def _report(self, rid: int, payload: bytes, length: int) -> None:
        os.write(self.fd, bytes([rid]) + payload.ljust(length, b"\x00"))

    def show(self, buf: list[int]) -> None:
        self._report(0xA0, b"\x00\x00", self.len_a0)
        try:
            self._report(0x82, bytes(buf), self.nkeys)
        finally:
            self._report(0xA0, b"\x01\x00", self.len_a0)

    def close(self) -> None:
        try:
            self.show([OFF] * self.nkeys)
        except OSError:
            pass  # keyboard already gone
        finally:
            os.close(self.fd)

    def describe(self) -> str:
        note = "" if self.pid in TESTED else " (untested model, please report back)"
        return f"{self.model} at {self.path}, {self.nkeys} keys{note}"


# --------------------------------------------------------------------------
# MIDI -> LED state (no hardware, testable)
# --------------------------------------------------------------------------

GUIDE_CHANNEL = 16  # note on (any velocity, even 0!) = on, note off = off
GUIDE_AT_CHANNEL = 3  # poly aftertouch: data1 = key index from A0, value > 0 = on
A0 = 21


class KeyState:
    """Which key is lit in which colour.

    Two layers, derived from a capture of PianoGlow:
      guide:  channel 16 note on/off and channel 3 poly aftertouch. PianoGlow sends
              both for the same key, so they share one set.
      played: note on/off on every other channel, regular MIDI semantics.
    The guide wins over a played note on the same key.
    """

    def __init__(self, lowest: int, nkeys: int, guide_color: int, play_color: int):
        self.lowest = lowest
        self.nkeys = nkeys
        self.guide_color = guide_color
        self.play_color = play_color
        self.guide: set[int] = set()
        self.played: dict[int, set[int]] = {}  # channel -> notes

    def _snapshot(self):
        return frozenset(self.guide), frozenset((c, n) for c, ns in self.played.items() for n in ns)

    def handle(self, msg: list[int]) -> bool:
        """Process one MIDI message. Returns True if the state changed."""
        if len(msg) < 3 or msg[0] >= 0xF0:
            return False
        status, ch, d1, d2 = msg[0] & 0xF0, (msg[0] & 0x0F) + 1, msg[1], msg[2]
        before = self._snapshot()

        if ch == GUIDE_CHANNEL and status == 0x90:
            self.guide.add(d1)
        elif ch == GUIDE_CHANNEL and status == 0x80:
            self.guide.discard(d1)
        elif ch == GUIDE_AT_CHANNEL and status == 0xA0:
            (self.guide.add if d2 > 0 else self.guide.discard)(A0 + d1)
        elif status == 0x90 and d2 > 0:
            self.played.setdefault(ch, set()).add(d1)
        elif status == 0x80 or status == 0x90:
            self.played.get(ch, set()).discard(d1)
        elif status == 0xB0 and d1 in (120, 123):
            if ch == GUIDE_CHANNEL:
                self.guide.clear()
            else:
                self.played.pop(ch, None)

        return self._snapshot() != before

    def clear(self) -> None:
        self.guide.clear()
        self.played.clear()

    def buffer(self) -> list[int]:
        buf = [OFF] * self.nkeys

        def put(note: int, color: int) -> None:
            idx = note - self.lowest
            if 0 <= idx < self.nkeys:
                buf[idx] = color

        for notes in self.played.values():
            for note in notes:
                put(note, self.play_color)
        for note in self.guide:
            put(note, self.guide_color)
        return buf


def default_lowest(nkeys: int) -> int:
    """MIDI note of the leftmost key without octave shift: A0 on 88 keys, C2 otherwise."""
    return A0 if nkeys >= 88 else 36


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------


def describe(msg: list[int]) -> str:
    status = msg[0] & 0xF0
    ch = (msg[0] & 0x0F) + 1
    names = {
        0x80: "NoteOff",
        0x90: "NoteOn",
        0xA0: "PolyAT",
        0xB0: "CC",
        0xC0: "Prog",
        0xD0: "ChanAT",
        0xE0: "Bend",
    }
    hexs = " ".join(f"{b:02X}" for b in msg)
    if msg[0] >= 0xF0:
        return f"{hexs}  System"
    return f"{hexs}  {names.get(status, '?')} ch{ch} " + " ".join(str(b) for b in msg[1:])


def open_virtual_in(name: str):
    import rtmidi

    midi_in = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name="kontrol-lightguide")
    midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
    midi_in.open_virtual_port(name)
    q: queue.Queue = queue.Queue()
    midi_in.set_callback(lambda event, _data: q.put(event[0]))
    return midi_in, q


def run_dump(args) -> None:
    midi_in, q = open_virtual_in(args.port)
    print(f"Virtual port '{args.port}' open, logging. Ctrl+C to stop.", flush=True)
    t0 = time.monotonic()
    try:
        while True:
            msg = q.get()
            print(f"{time.monotonic() - t0:8.3f}s  {describe(msg)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        midi_in.close_port()


def run_bridge(args) -> None:
    lg = LightGuide(args.device)
    lowest = args.lowest if args.lowest is not None else default_lowest(lg.nkeys)
    state = KeyState(lowest, lg.nkeys, COLORS[args.guide_color], COLORS[args.play_color])
    midi_in, q = open_virtual_in(args.port)
    print(f"{lg.describe()}, port '{args.port}', leftmost key = note {lowest}.")
    print("Ctrl+C to stop.", flush=True)
    last = None
    try:
        while True:
            msg = q.get()
            changed = state.handle(msg)
            # collect briefly: send chords and clear bursts as one report,
            # every report pauses key MIDI for a moment
            deadline = time.monotonic() + args.coalesce
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    changed |= state.handle(q.get(timeout=remaining))
                except queue.Empty:
                    break
            if args.verbose:
                print(describe(msg), flush=True)
            buf = state.buffer()
            if changed and buf != last:
                lg.show(buf)
                last = buf
    except KeyboardInterrupt:
        pass
    except OSError as e:
        sys.exit(f"lost the keyboard ({e}), exiting")
    finally:
        midi_in.close_port()
        lg.close()


def run_calibrate(args) -> None:
    """Measure the note of the leftmost key (depends on the octave shift)."""
    import rtmidi

    midi_in = rtmidi.MidiIn(rtmidi.API_LINUX_ALSA, name="kontrol-lightguide-cal")
    ports = midi_in.get_ports()
    idx = next((i for i, p in enumerate(ports) if "kontrol" in p.lower() and "main" in p.lower()), None)
    if idx is None:
        idx = next((i for i, p in enumerate(ports) if "kontrol" in p.lower()), None)
    if idx is None:
        sys.exit(f"no Kontrol MIDI input found: {ports}")
    midi_in.open_port(idx)
    print(f"Listening on '{ports[idx]}'. Press the LEFTMOST key ({args.timeout:.0f} s).", flush=True)
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            ev = midi_in.get_message()
            if ev is None:
                time.sleep(0.005)
                continue
            msg = ev[0]
            if len(msg) >= 3 and msg[0] & 0xF0 == 0x90 and msg[2] > 0:
                print(f"LOWEST={msg[1]}  (use: bridge --lowest {msg[1]})", flush=True)
                lg = LightGuide(args.device)
                buf = [OFF] * lg.nkeys
                buf[0] = COLORS["green"]
                lg.show(buf)
                time.sleep(1.5)
                lg.close()
                return
        sys.exit("no key press detected")
    finally:
        midi_in.close_port()


def run_test(args) -> None:
    lg = LightGuide(args.device)
    print(f"{lg.describe()}: running light across all keys.")
    try:
        for i in range(lg.nkeys):
            buf = [OFF] * lg.nkeys
            buf[i] = COLORS["green"]
            lg.show(buf)
            time.sleep(0.04)
    finally:
        lg.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", default=PORT_NAME, help="name of the virtual MIDI port")
    p.add_argument("--device", help="hidraw node (default: auto-detect)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("dump", help="only log what arrives at the virtual port")
    sub.add_parser("test", help="running light across all keys")
    c = sub.add_parser("calibrate", help="measure the note of the leftmost key (octave shift)")
    c.add_argument("--timeout", type=float, default=60.0)

    b = sub.add_parser("bridge", help="show notes from the virtual port on the light guide")
    b.add_argument(
        "--lowest",
        type=int,
        help="MIDI note of the leftmost key (default: 36 = C2, on 88 keys 21 = A0)",
    )
    b.add_argument("--guide-color", default="blue", choices=COLORS, help="colour of the guide (ch 16/3)")
    b.add_argument("--play-color", default="green", choices=COLORS, help="colour of played notes")
    b.add_argument("--coalesce", type=float, default=0.010, help="collect window in seconds")
    b.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "dump":
        run_dump(args)
    elif args.cmd == "test":
        run_test(args)
    elif args.cmd == "calibrate":
        run_calibrate(args)
    elif args.cmd == "bridge":
        run_bridge(args)
    else:
        p.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()

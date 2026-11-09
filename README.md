# kontrol-lightguide

Drive the light guide of a Native Instruments **Kontrol S-Series MK3** keyboard on Linux, without any NI software.

The keyboard ignores light commands sent over MIDI. On macOS and Windows the light guide is controlled by NI's background service, which does not exist for Linux. This tool creates a virtual ALSA MIDI port called **Kontrol Light Guide** and turns whatever arrives there into HID reports for the keyboard. Apps that can light keys on other keyboards (PianoGlow, for example) can then use it as their MIDI output.

## Status

| Model | USB ID | Status |
|---|---|---|
| Kontrol S61 MK3 | `17cc:2110` | works, tested with PianoGlow, keyboard in MIDI 1.0 and MIDI 2.0 mode |
| Kontrol S49 MK3 | `17cc:2100` | untested |
| Kontrol S88 MK3 | `17cc:2120` | untested here, the protocol was found on this model |

The number of keys and the report lengths are read from the HID descriptor, so S49 and S88 should work. If you try one of them, please open an issue with the result.

Not tested yet: playing while keys are lit. Every LED update briefly switches the keyboard into LED mode, and key MIDI can get lost during that window (on the S88, note offs were dropped now and then). The bridge collects messages for 10 ms and only sends a report when something changed.

## Protocol

HID output reports on interface 2 (`/dev/hidrawN`, found automatically). Reports are padded with zeros to the length from the descriptor.

| Report | Length on S61 | Meaning |
|---|---|---|
| `A0 00 00` | 14 | enter LED mode, key MIDI pauses |
| `82` + one byte per key | 61 | colour per key, index 0 = leftmost key |
| `A0 01 00` | 14 | leave LED mode, MIDI works again, LEDs stay lit |

Colours: `06` red, `0C` orange, `12` yellow, `1E` green, `2C` blue, `7F` white.

The protocol was worked out by [ca9](https://github.com/ca9) on an S88 MK3 in [KompleteSynthesia discussion #29](https://github.com/tillt/KompleteSynthesia/discussions/29). This project only reuses that knowledge, no code.

## MIDI at the virtual port

Derived from a capture of what PianoGlow sends:

| Layer | Message | Effect |
|---|---|---|
| guide (blue) | channel 16 note on, **any velocity, including 0** | key on |
| guide | channel 16 note off | key off |
| guide | channel 3 poly aftertouch, data1 = key index from A0, value > 0 / 0 | on / off |
| played (green) | note on/off on all other channels | regular MIDI meaning |
| | CC 120 / 123 | clears the layer of that channel |

PianoGlow sends channel 16 and channel 3 at the same time for the same key, both end up in the same set. If another app does not light anything, run `dump` and look at what it sends.

## Setup

hidraw nodes belong to root. The udev rule gives the logged-in user access:

```bash
sudo install -m 0644 udev/62-kontrol-mk3.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw
```

Python with [python-rtmidi](https://pypi.org/project/python-rtmidi/):

```bash
uv venv .venv
uv pip install --python .venv/bin/python python-rtmidi pytest
```

## Usage

```bash
.venv/bin/python kontrol_lightguide.py test        # running light across all keys
.venv/bin/python kontrol_lightguide.py calibrate   # measure the note of the leftmost key
.venv/bin/python kontrol_lightguide.py dump        # only log what arrives at the port
.venv/bin/python kontrol_lightguide.py bridge      # the actual bridge
```

Then pick **Kontrol Light Guide** as MIDI output in your app.

`bridge --lowest N` sets the MIDI note of the leftmost key. Without octave shift that is 36 (C2) on the S49 and S61 and 21 (A0) on the S88, which is also the default. If you use the octave buttons, run `calibrate` and pass the result.

Options: `--guide-color`, `--play-color` (red, orange, yellow, green, blue, white), `--port`, `--device`, `-v`.

## Tests

```bash
.venv/bin/python -m pytest -q tests
```

## License

MIT

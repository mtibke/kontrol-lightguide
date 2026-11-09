import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kontrol_lightguide import COLORS, OFF, KeyState, default_lowest, output_report_sizes  # noqa: E402

GUIDE, PLAY = COLORS["blue"], COLORS["green"]
NKEYS = 61


def make(lowest=36, nkeys=NKEYS):
    return KeyState(lowest=lowest, nkeys=nkeys, guide_color=GUIDE, play_color=PLAY)


def lit(state):
    return {i: c for i, c in enumerate(state.buffer()) if c != OFF}


def test_buffer_has_one_byte_per_key():
    assert len(make().buffer()) == NKEYS
    assert len(make(nkeys=88).buffer()) == 88


def test_ch16_note_on_with_velocity_0_lights_the_key():
    # PianoGlow: 9F 3C 00 means "key 60 on", not "off"
    s = make()
    assert s.handle([0x9F, 60, 0])
    assert lit(s) == {24: GUIDE}


def test_ch16_note_off_clears():
    s = make()
    s.handle([0x9F, 60, 0])
    assert s.handle([0x8F, 60, 0])
    assert lit(s) == {}


def test_ch3_poly_aftertouch_is_key_index_from_a0():
    # A2 27 01: index 39 from A0 (21) = note 60
    s = make()
    assert s.handle([0xA2, 39, 1])
    assert lit(s) == {24: GUIDE}
    assert s.handle([0xA2, 39, 0])
    assert lit(s) == {}


def test_ch16_and_ch3_for_the_same_key_are_one_key():
    s = make()
    s.handle([0x9F, 60, 0])
    assert not s.handle([0xA2, 39, 1])
    s.handle([0x8F, 60, 0])
    assert not s.handle([0xA2, 39, 0])
    assert lit(s) == {}


def test_played_note_on_channel_1():
    s = make()
    assert s.handle([0x90, 64, 96])
    assert lit(s) == {28: PLAY}
    assert s.handle([0x90, 64, 0])  # regular MIDI: velocity 0 = off
    assert lit(s) == {}


def test_guide_wins_over_played_note():
    s = make()
    s.handle([0x90, 60, 96])
    s.handle([0x9F, 60, 0])
    assert lit(s) == {24: GUIDE}


def test_notes_outside_the_keyboard_are_ignored():
    s = make(lowest=36)
    s.handle([0x9F, 35, 0])
    s.handle([0x9F, 97, 0])
    assert lit(s) == {}
    s.handle([0x9F, 36, 0])
    s.handle([0x9F, 96, 0])
    assert lit(s) == {0: GUIDE, 60: GUIDE}


def test_all_notes_off_only_clears_its_channel():
    s = make()
    s.handle([0x90, 64, 96])
    s.handle([0x9F, 60, 0])
    assert s.handle([0xB0, 123, 0])
    assert lit(s) == {24: GUIDE}


def test_unknown_messages_do_nothing():
    s = make()
    assert not s.handle([])
    assert not s.handle([0xF8])
    assert not s.handle([0xE0, 0, 64])


def test_default_lowest():
    assert default_lowest(49) == 36
    assert default_lowest(61) == 36
    assert default_lowest(88) == 21


def test_output_report_sizes():
    # minimal descriptor: two output reports, 0x82 with 61 bytes, 0xA0 with 14 bytes,
    # plus an input report that must not count
    desc = bytes(
        [
            0x06, 0x00, 0xFF,  # Usage Page (vendor)
            0x09, 0x01,  # Usage
            0xA1, 0x01,  # Collection (Application)
            0x85, 0x01, 0x75, 0x08, 0x95, 0x05, 0x81, 0x02,  # ID 1, 8 bit x 5, Input
            0x85, 0x82, 0x75, 0x08, 0x95, 0x3D, 0x91, 0x02,  # ID 0x82, 8 bit x 61, Output
            0x85, 0xA0, 0x75, 0x08, 0x95, 0x0E, 0x91, 0x02,  # ID 0xA0, 8 bit x 14, Output
            0xC0,  # End Collection
        ]
    )  # fmt: skip
    assert output_report_sizes(desc) == {0x82: 61, 0xA0: 14}

import random

from lloydplus.framing import FrameDecoder, crc16_arc, encode_frame


def test_crc16_arc_check_value():
    assert crc16_arc(b"123456789") == 0xBB3D


def test_escaping():
    body = bytes([0x02, 0xDC, 0xDD, 0x00, 0x5C])
    wire = encode_frame(body)
    assert wire[0] == 0xDC
    assert 0xDC not in wire[1:]
    assert FrameDecoder().feed(wire) == [body]


def test_stream_and_garbage():
    rng = random.Random(1)
    bodies = [bytes([rng.choice([2, 0xCE])]) + rng.randbytes(rng.randrange(0, 200)) for _ in range(200)]
    wire = b"\x55\xdc\xdc" + b"".join(encode_frame(b) for b in bodies)
    dec = FrameDecoder()
    out = []
    for i in range(0, len(wire), 7):
        out += dec.feed(wire[i:i + 7])
    assert out == bodies


def test_corruption_detected():
    wire = bytearray(encode_frame(b"\x02\x01\x02\x03"))
    wire[-1] ^= 0x01
    dec = FrameDecoder()
    assert dec.feed(bytes(wire) + encode_frame(b"\x02\x09")) == [b"\x02\x09"]
    assert dec.crc_errors == 1


def test_atof_semantics():
    from lloydplus.services import atof
    assert atof(b"1.5x") == 1.5
    assert atof(b"  -2e3") == -2000.0
    assert atof(b"abc") == 0.0
    assert atof(b".5") == 0.5

import fractions
import logging
import math
from itertools import tee
from struct import pack, unpack_from
from typing import Iterator, List, Optional, Sequence, Tuple, Type, TypeVar

import av
from av.frame import Frame

from ..jitterbuffer import JitterFrame
from ..mediastreams import VIDEO_TIME_BASE, convert_timebase
from .base import Decoder, Encoder


logger = logging.getLogger("codec.h264")

MAX_FRAME_RATE = 30
PACKET_MAX = 1300

NAL_TYPE_FU_A = 28
NAL_TYPE_STAP_A = 24

NAL_HEADER_SIZE = 1
FU_A_HEADER_SIZE = 2
LENGTH_FIELD_SIZE = 2
STAP_A_HEADER_SIZE = NAL_HEADER_SIZE + LENGTH_FIELD_SIZE

DESCRIPTOR_T = TypeVar("DESCRIPTOR_T", bound="H264PayloadDescriptor")
T = TypeVar("T")


def pairwise(iterable: Sequence[T]) -> Iterator[Tuple[T, T]]:
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


class H264PayloadDescriptor:
    def __init__(self, first_fragment):
        self.first_fragment = first_fragment

    def __repr__(self):
        return f"H264PayloadDescriptor(FF={self.first_fragment})"

    @classmethod
    def parse(cls: Type[DESCRIPTOR_T], data: bytes) -> Tuple[DESCRIPTOR_T, bytes]:
        output = bytes()

        # NAL unit header
        if len(data) < 2:
            raise ValueError("NAL unit is too short")
        nal_type = data[0] & 0x1F
        f_nri = data[0] & (0x80 | 0x60)
        pos = NAL_HEADER_SIZE

        if nal_type in range(1, 24):
            # single NAL unit
            output = bytes([0, 0, 0, 1]) + data
            obj = cls(first_fragment=True)
        elif nal_type == NAL_TYPE_FU_A:
            # fragmentation unit
            original_nal_type = data[pos] & 0x1F
            first_fragment = bool(data[pos] & 0x80)
            pos += 1

            if first_fragment:
                original_nal_header = bytes([f_nri | original_nal_type])
                output += bytes([0, 0, 0, 1])
                output += original_nal_header
            output += data[pos:]

            obj = cls(first_fragment=first_fragment)
        elif nal_type == NAL_TYPE_STAP_A:
            # single time aggregation packet
            offsets = []
            while pos < len(data):
                if len(data) < pos + LENGTH_FIELD_SIZE:
                    raise ValueError("STAP-A length field is truncated")
                nulu_size = unpack_from("!H", data, pos)[0]
                pos += LENGTH_FIELD_SIZE
                offsets.append(pos)

                pos += nulu_size
                if len(data) < pos:
                    raise ValueError("STAP-A data is truncated")

            offsets.append(len(data) + LENGTH_FIELD_SIZE)
            for start, end in pairwise(offsets):
                end -= LENGTH_FIELD_SIZE
                output += bytes([0, 0, 0, 1])
                output += data[start:end]

            obj = cls(first_fragment=True)
        else:
            raise ValueError(f"NAL unit type {nal_type} is not supported")

        return obj, output


class H264Decoder(Decoder):
    def __init__(self) -> None:
        self.codec = av.CodecContext.create("h264", "r")

    def decode(self, encoded_frame: JitterFrame) -> List[Frame]:
        try:
            packet = av.Packet(encoded_frame.data)
            packet.pts = encoded_frame.timestamp
            packet.time_base = VIDEO_TIME_BASE
            frames = self.codec.decode(packet)
        except av.AVError as e:
            logger.warning("failed to decode, skipping package: " + str(e))
            return []

        return frames


class H264Encoder(Encoder):
    def __init__(self) -> None:
        self.codec: Optional[av.CodecContext] = None

    @staticmethod
    def _packetize_fu_a(data: bytes) -> List[bytes]:
        available_size = PACKET_MAX - FU_A_HEADER_SIZE
        payload_size = len(data) - NAL_HEADER_SIZE
        num_packets = math.ceil(payload_size / available_size)
        num_larger_packets = payload_size % num_packets
        package_size = payload_size // num_packets

        f_nri = data[0] & (0x80 | 0x60)  # fni of original header
        nal = data[0] & 0x1F

        fu_indicator = f_nri | NAL_TYPE_FU_A

        fu_header_end = bytes([fu_indicator, nal | 0x40])
        fu_header_middle = bytes([fu_indicator, nal])
        fu_header_start = bytes([fu_indicator, nal | 0x80])
        fu_header = fu_header_start

        packages = []
        offset = NAL_HEADER_SIZE
        while offset < len(data):
            if num_larger_packets > 0:
                num_larger_packets -= 1
                payload = data[offset : offset + package_size + 1]
                offset += package_size + 1
            else:
                payload = data[offset : offset + package_size]
                offset += package_size

            if offset == len(data):
                fu_header = fu_header_end

            packages.append(fu_header + payload)

            fu_header = fu_header_middle
        assert offset == len(data), "incorrect fragment data"

        return packages

    @staticmethod
    def _packetize_stap_a(
            data: bytes, packages_iterator: Iterator[bytes]
    ) -> Tuple[bytes, bytes]:
        counter = 0
        available_size = PACKET_MAX - STAP_A_HEADER_SIZE

        stap_header = NAL_TYPE_STAP_A | (data[0] & 0xE0)

        payload = bytes()
        try:
            nalu = data  # with header
            while len(nalu) <= available_size:
                stap_header |= nalu[0] & 0x80

                nri = nalu[0] & 0x60
                if stap_header & 0x60 < nri:
                    stap_header = stap_header & 0x9F | nri

                available_size -= LENGTH_FIELD_SIZE + len(nalu)
                counter += 1
                payload += pack("!H", len(nalu)) + nalu
                nalu = next(packages_iterator)

            if counter == 0:
                nalu = next(packages_iterator)
        except StopIteration:
            nalu = None

        if counter <= 1:
            return data, nalu
        else:
            return bytes([stap_header]) + payload, nalu

    @staticmethod
    def _split_bitstream(buf: bytes) -> Iterator[bytes]:
        # TODO: write in a more pytonic way,
        # translate from: https://github.com/aizvorski/h264bitstream/blob/master/h264_nal.c#L134
        i = 0
        while True:
            while (buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01) and (
                    buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0 or buf[i + 3] != 0x01
            ):
                i += 1  # skip leading zero
                if i + 4 >= len(buf):
                    return
            if buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01:
                i += 1
            i += 3
            nal_start = i
            while (buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0) and (
                    buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01
            ):
                i += 1
                # FIXME: the next line fails when reading a nal that ends
                # exactly at the end of the data
                if i + 3 >= len(buf):
                    nal_end = len(buf)
                    yield buf[nal_start:nal_end]
                    return  # did not find nal end, stream ended first
            nal_end = i
            yield buf[nal_start:nal_end]

    @classmethod
    def _packetize(cls, packages: Iterator[bytes]) -> List[bytes]:
        packetized_packages = []

        packages_iterator = iter(packages)
        package = next(packages_iterator, None)
        while package is not None:
            if len(package) > PACKET_MAX:
                packetized_packages.extend(cls._packetize_fu_a(package))
                package = next(packages_iterator, None)
            else:
                packetized, package = cls._packetize_stap_a(package, packages_iterator)
                packetized_packages.append(packetized)

        return packetized_packages

    def _encode_frame(
            self, frame: av.VideoFrame, force_keyframe: bool
    ) -> Iterator[bytes]:
        if self.codec and (
                frame.width != self.codec.width or frame.height != self.codec.height
        ):
            self.codec = None

        if self.codec is None:
            self.codec = av.CodecContext.create("libx264", "w")
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.pix_fmt = "yuv420p"
            self.codec.time_base = fractions.Fraction(1, MAX_FRAME_RATE)
            self.codec.options = {
                "profile": "baseline",
                "level": "31",
                "tune": "zerolatency",
                "preset":"ultrafast",
            }

        packages = self.codec.encode(frame)
        yield from self._split_bitstream(b"".join(p.to_bytes() for p in packages))

    def encode(
            self, frame: Frame, force_keyframe: bool = False
    ) -> Tuple[List[bytes], int]:
        assert isinstance(frame, av.VideoFrame)
        packages = self._encode_frame(frame, force_keyframe)
        timestamp = convert_timebase(frame.pts, frame.time_base, VIDEO_TIME_BASE)
        return self._packetize(packages), timestamp



class H264Packer(Encoder):

    def __init__(self) -> None:
        self.codec: Optional[av.CodecContext] = None

    @staticmethod
    def _packetize_fu_a(data: bytes) -> List[bytes]:
        available_size = PACKET_MAX - FU_A_HEADER_SIZE
        payload_size = len(data) - NAL_HEADER_SIZE
        num_packets = math.ceil(payload_size / available_size)
        num_larger_packets = payload_size % num_packets
        package_size = payload_size // num_packets

        f_nri = data[0] & (0x80 | 0x60)  # fni of original header
        nal = data[0] & 0x1F

        fu_indicator = f_nri | NAL_TYPE_FU_A

        fu_header_end = bytes([fu_indicator, nal | 0x40])
        fu_header_middle = bytes([fu_indicator, nal])
        fu_header_start = bytes([fu_indicator, nal | 0x80])
        fu_header = fu_header_start

        packages = []
        offset = NAL_HEADER_SIZE
        while offset < len(data):
            if num_larger_packets > 0:
                num_larger_packets -= 1
                payload = data[offset : offset + package_size + 1]
                offset += package_size + 1
            else:
                payload = data[offset : offset + package_size]
                offset += package_size

            if offset == len(data):
                fu_header = fu_header_end

            packages.append(fu_header + payload)

            fu_header = fu_header_middle
        assert offset == len(data), "incorrect fragment data"

        return packages

    @staticmethod
    def _packetize_stap_a(
            data: bytes, packages_iterator: Iterator[bytes]
    ) -> Tuple[bytes, bytes]:
        counter = 0
        available_size = PACKET_MAX - STAP_A_HEADER_SIZE

        stap_header = NAL_TYPE_STAP_A | (data[0] & 0xE0)

        payload = bytes()
        try:
            nalu = data  # with header
            while len(nalu) <= available_size:
                stap_header |= nalu[0] & 0x80

                nri = nalu[0] & 0x60
                if stap_header & 0x60 < nri:
                    stap_header = stap_header & 0x9F | nri

                available_size -= LENGTH_FIELD_SIZE + len(nalu)
                counter += 1
                payload += pack("!H", len(nalu)) + nalu
                nalu = next(packages_iterator)

            if counter == 0:
                nalu = next(packages_iterator)
        except StopIteration:
            nalu = None

        if counter <= 1:
            return data, nalu
        else:
            return bytes([stap_header]) + payload, nalu

    @staticmethod
    def _split_bitstream(buf: bytes) -> Iterator[bytes]:
        # TODO: write in a more pytonic way,
        # translate from: https://github.com/aizvorski/h264bitstream/blob/master/h264_nal.c#L134
        i = 0
        while True:
            #print(str(len(buf)))
            #print(i)
            if(len(buf)>=1+3):
                while (buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01) and (
                        buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0 or buf[i + 3] != 0x01
                ):
                    i += 1  # skip leading zero
                    if i + 4 >= len(buf):
                        return
                if buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01:
                    i += 1
                i += 3
                nal_start = i
                while (buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0) and (
                        buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0x01
                ):
                    i += 1
                    # FIXME: the next line fails when reading a nal that ends
                    # exactly at the end of the data
                    if i + 3 >= len(buf):
                        nal_end = len(buf)
                        yield buf[nal_start:nal_end]
                        return  # did not find nal end, stream ended first
                nal_end = i
                yield buf[nal_start:nal_end]

    @classmethod
    def _packetize(cls, packages: Iterator[bytes]) -> List[bytes]:
        packetized_packages = []

        packages_iterator = iter(packages)
        package = next(packages_iterator, None)
        while package is not None:
            if len(package) > PACKET_MAX:
                packetized_packages.extend(cls._packetize_fu_a(package))
                package = next(packages_iterator, None)
            else:
                packetized, package = cls._packetize_stap_a(package, packages_iterator)
                packetized_packages.append(packetized)

        return packetized_packages

    def _encode_frame(
            self, frame:bytes, force_keyframe: bool
    ) -> Iterator[bytes]:
        if self.codec and (
                frame.width != self.codec.width or frame.height != self.codec.height
        ):
            self.codec = None



        yield from self._split_bitstream(frame)

    def encode(
            self, frame:bytes, force_keyframe: bool = False
    ) -> Tuple[List[bytes], int]:
        packages = self._encode_frame(frame, force_keyframe)
        timestamp = convert_timebase(0,fractions.Fraction(1, 90000) , VIDEO_TIME_BASE)
        return self._packetize(packages), timestamp


def h264_depayload(payload: bytes) -> bytes:
    descriptor, data = H264PayloadDescriptor.parse(payload)
    return data
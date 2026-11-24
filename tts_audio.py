"""Join independently encoded Kokoro segments into one live MPEG audio stream."""
from tts_service import TTSError


_BIT_RATES = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_RATE_DIVISOR = {3: 1, 2: 2, 0: 4}


def _invalid():
    return TTSError("TTS_INVALID_AUDIO", "语音服务返回的音频无效，请重试", 502)


class MP3Stream:
    def __init__(self, output, max_bytes, publish):
        self.output = output
        self.max_bytes = max_bytes
        self.publish = publish
        self.format = None
        self.start_segment()

    def start_segment(self):
        self.pending = bytearray()
        self.skip = 0
        self.first_frame = True
        self.written_frame = False

    def write(self, chunk):
        before = self.output.tell()
        self.pending.extend(chunk)
        while self.pending:
            if self.skip:
                amount = min(self.skip, len(self.pending))
                del self.pending[:amount]
                self.skip -= amount
                continue
            if len(self.pending) < 4:
                break
            if self.pending.startswith(b"ID3"):
                if len(self.pending) < 10:
                    break
                tag_size = self.pending[6:10]
                if any(value > 127 for value in tag_size):
                    raise _invalid()
                self.skip = 10 + (10 if self.pending[3] == 4 and self.pending[5] & 16 else 0)
                self.skip += sum(value << shift for value, shift in zip(tag_size, (21, 14, 7, 0)))
                continue
            if self.pending.startswith(b"TAG"):
                self.skip = 128
                continue
            sync, flags, rate, channels = self.pending[:4]
            version, layer = (flags >> 3) & 3, (flags >> 1) & 3
            bit_rate, sample = rate >> 4, (rate >> 2) & 3
            if (sync != 255 or flags & 224 != 224 or version not in _RATE_DIVISOR or layer != 1
                    or not 1 <= bit_rate <= 14 or sample == 3):
                raise _invalid()
            frequency = (44100, 48000, 32000)[sample] // _RATE_DIVISOR[version]
            mpeg1 = version == 3
            mono = channels >> 6 == 3
            length = (144000 if mpeg1 else 72000) * _BIT_RATES[3 if mpeg1 else 2][bit_rate] // frequency
            length += (rate >> 1) & 1
            if len(self.pending) < length:
                break
            frame = self.pending[:length]
            del self.pending[:length]
            if self.first_frame:
                self.first_frame = False
                side_info = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
                marker = 4 + (0 if flags & 1 else 2) + side_info
                if frame[marker:marker + 4] in (b"Xing", b"Info") or frame[36:40] == b"VBRI":
                    # An encoder index describes its original segment only.
                    continue
            current_format = (frequency, mono)
            if self.format is None:
                self.format = current_format
            if self.format != current_format:
                raise _invalid()
            if self.output.tell() + length > self.max_bytes:
                raise TTSError("TTS_AUDIO_TOO_LARGE", "生成的语音过大，请稍后重试", 502)
            self.output.write(frame)
            self.written_frame = True
        if self.output.tell() != before:
            self.output.flush()
            self.publish(self.output.tell())

    def finish_segment(self):
        if self.skip or self.pending or not self.written_frame:
            raise _invalid()

"""文字起こし（faster-whisper）。ai-callcenter-ollama の voice/stt_whisper.py をもとに、会議向けに広げた。

- CPU・int8 で動かす（GPUのないPCでも動く）。精度が足りなければ medium のモデルにする
- 無音の部分を飛ばす（VAD）。長い会議を短くし、無音を言葉と聞き違えるのも減らす
- 動画（mp4 など）もそのまま入れられる。音声は、ここで PyAV を使って取り出してから faster-whisper に渡す
  （faster-whisper 1.2.1 の取り出しは、PyAV 19 でなくなった引数 metadata_errors を使っていて止まるため）
- 言葉のヒント（initial_prompt）は、会社の用語集・部署名・社員の名前から作る
- モデルは models pull で入手・記録したものを、ハッシュで照合してから使う。ここでは通信しない
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

PROMPT_MAX = 220   # ヒントが長すぎると、かえって認識が崩れるので短く保つ


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def build_prompt(glossary: list[str], names: list[str], depts: list[str], max_chars: int = PROMPT_MAX) -> str:
    """会議でよく出る言葉を、ヒントの文にする（用語集 → 部署名 → 社員の名前 の順に、長さの上限まで）。"""
    words: list[str] = []
    for w in [*glossary, *depts, *names]:
        w = w.strip()
        if w and w not in words and len("、".join([*words, w])) <= max_chars:
            words.append(w)
    return "、".join(words)


def read_glossary(path: Path | None) -> list[str]:
    if not path:
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


SAMPLE_RATE = 16000   # faster-whisper が受け取る音声の速さ


def decode_audio(media: Path, sampling_rate: int = SAMPLE_RATE):
    """動画・音声のファイルから、1チャンネル・16kHz の音声（float32 の配列）を取り出す。"""
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    chunks = []
    with av.open(str(media), mode="r") as container:
        if not container.streams.audio:
            raise ValueError(f"{media.name} に音声がありません")
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):   # 残りを出し切る
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class WhisperTranscriber:
    def __init__(self, model_dir: Path, device: str = "cpu", compute_type: str = "int8"):
        from faster_whisper import WhisperModel

        from ..netpolicy import offline

        offline()   # ここでは通信しない（モデルはフォルダから読む）
        self.model = WhisperModel(str(model_dir), device=device, compute_type=compute_type, local_files_only=True)

    def transcribe(self, media: Path, prompt: str = "") -> tuple[list[Segment], dict]:
        start = time.time()
        segments, info = self.model.transcribe(
            decode_audio(media), language="ja", beam_size=5, vad_filter=True,
            initial_prompt=prompt or None,
            condition_on_previous_text=False,   # 同じ言葉の繰り返し（幻聴）が続くのを防ぐ
        )
        out = [Segment(round(s.start, 2), round(s.end, 2), s.text.strip()) for s in segments if s.text.strip()]
        return out, {"duration": round(info.duration, 1), "elapsed": round(time.time() - start, 1),
                     "language": info.language}


def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"

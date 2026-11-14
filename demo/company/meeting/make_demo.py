"""デモ用の会議の録画「風」の動画を作る（Zoom風の画面。前半はスピーカービュー、後半はギャラリービュー）。

    python demo/company/meeting/make_demo.py              # 声なし（話し手の読み取りの確認用）
    python demo/company/meeting/make_demo.py --voicevox   # VOICEVOX（この端末で起動しているもの）で声を入れる

できるもの:
    demo_zoom.mp4            会議の録画風の動画
    demo_zoom_segments.json  台本どおりの発言と時刻（文字起こしの代わりに使える。テストもこれを使う）
台本・人物・会社はすべて架空。VOICEVOX は http://127.0.0.1:50021 （この端末の中）にだけ通信する。
"""

import argparse
import io
import json
import sys
import wave
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakeui  # noqa: E402

OUT = Path(__file__).resolve().parent
RATE = 24000
FPS = 5
PEOPLE = ["佐藤 花子", "田中 健", "山田 太郎", "本社5F会議室"]
VOICE = {0: 2, 1: 13, 2: 11, 3: 3}   # VOICEVOX の話者の番号（人ごとに声を変える）
SCRIPT = [   # (話し手の番号, 画面の並べ方, 発言)
    (0, "speaker", "それでは本社移転の定例会を始めます。今日は工事の日程と複合機の移設について決めます。"),
    (1, "speaker", "ネットワーク工事は十二月の第二週に行う予定です。土曜日の作業になります。"),
    (2, "speaker", "工事の費用は今月の予算の範囲で収まりますか。"),
    (1, "speaker", "はい、見積は前回の金額から変わっていません。"),
    (0, "gallery", "では工事は十二月の第二週の土曜日で決定します。田中さん、当日の作業手順をまとめてください。"),
    (1, "gallery", "わかりました。来週の金曜日までにまとめます。"),
    (3, "gallery", "会議室から失礼します。複合機の移設はリース会社への連絡が必要です。"),
    (0, "gallery", "それは私がリース会社に連絡します。次回は十一月二十六日に開催します。"),
]


def voicevox(text: str, speaker: int) -> np.ndarray:
    import urllib.parse
    import urllib.request

    base = "http://127.0.0.1:50021"
    q = urllib.request.urlopen(urllib.request.Request(
        f"{base}/audio_query?{urllib.parse.urlencode({'text': text, 'speaker': speaker})}", method="POST")).read()
    wav = urllib.request.urlopen(urllib.request.Request(
        f"{base}/synthesis?speaker={speaker}", data=q, headers={"Content-Type": "application/json"})).read()
    with wave.open(io.BytesIO(wav)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getframerate() != RATE:   # 24kHz にそろえる（VOICEVOX は既定で 24kHz）
            idx = np.linspace(0, len(data) - 1, int(len(data) * RATE / w.getframerate()))
            data = np.interp(idx, np.arange(len(data)), data).astype(np.int16)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voicevox", action="store_true")
    args = ap.parse_args()

    clips, segments, t = [], [], 0.0
    for who, layout, text in SCRIPT:
        audio = voicevox(text, VOICE[who]) if args.voicevox else np.zeros(int(RATE * (1.0 + len(text) * 0.12)), np.int16)
        pause = np.zeros(int(RATE * 0.6), np.int16)
        dur = len(audio) / RATE
        segments.append({"start": round(t, 2), "end": round(t + dur, 2), "text": text, "speaker": "",
                         "expected": PEOPLE[who]})
        clips.append((who, layout, audio, pause))
        t += dur + len(pause) / RATE

    path = OUT / "demo_zoom.mp4"
    with av.open(str(path), "w") as out:
        v = out.add_stream("libx264", rate=FPS)
        v.width, v.height, v.pix_fmt = 1280, 720, "yuv420p"
        a = out.add_stream("aac", rate=RATE)
        a.layout = "mono"
        for who, layout, audio, pause in clips:
            frame = fakeui.render("zoom", layout, PEOPLE, who)
            n = int(round((len(audio) + len(pause)) / RATE * FPS))
            for _ in range(n):
                for p in v.encode(av.VideoFrame.from_image(frame)):
                    out.mux(p)
            pcm = np.concatenate([audio, pause])
            for i in range(0, len(pcm), 1024):
                af = av.AudioFrame.from_ndarray(pcm[i:i + 1024][np.newaxis, :], format="s16", layout="mono")
                af.sample_rate = RATE
                for p in a.encode(af):
                    out.mux(p)
        for p in v.encode():
            out.mux(p)
        for p in a.encode():
            out.mux(p)
    (OUT / "demo_zoom_segments.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{path}（{t:.0f}秒）と demo_zoom_segments.json を作りました" + ("（声あり）" if args.voicevox else "（声なし）"))


if __name__ == "__main__":
    main()

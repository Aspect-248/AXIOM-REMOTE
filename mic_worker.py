"""Standalone worker that records from the microphone and saves it as
a WAV file, run as a subprocess by bot.py with a hard timeout --
same reasoning as webcam_worker.py: a blocking audio-driver call that
hangs can't be interrupted from Python, so isolating it in its own
process with a timeout bounds the worst case instead of leaving the
mic indicator stuck on indefinitely.
"""
import sys
import wave

import sounddevice as sd


def main():
    out_path = sys.argv[1]
    duration_seconds = float(sys.argv[2])
    fs = 44100

    recording = sd.rec(int(duration_seconds * fs), samplerate=fs, channels=1, dtype="int16")
    sd.wait()

    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(fs)
        wf.writeframes(recording.tobytes())


if __name__ == "__main__":
    main()

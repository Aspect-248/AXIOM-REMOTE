"""Standalone worker that captures a single webcam frame and saves it
to the given path, run as a subprocess by bot.py with a hard timeout.

Why a subprocess: cv2.VideoCapture.read() can hang indefinitely on a
flaky camera driver -- observed directly, the webcam LED stayed on
for hours after a hung capture with no exception raised, nothing
recoverable in-process. A blocking C-level hang can't be interrupted
from Python even with try/finally or threading; the only reliable way
to bound it is to run it in a separate process and hard-kill that
process on timeout, which forcibly releases the camera handle at the
OS level.
"""
import sys

import cv2


def main():
    out_path = sys.argv[1]

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Could not open the webcam.", file=sys.stderr)
        sys.exit(1)

    try:
        # Discard the first few frames -- most webcams need a moment to
        # adjust exposure/focus, and the very first frame is often dark.
        for _ in range(5):
            cap.read()
        ok, frame = cap.read()
    finally:
        cap.release()

    if not ok:
        print("Failed to capture a frame from the webcam.", file=sys.stderr)
        sys.exit(1)

    cv2.imwrite(out_path, frame)


if __name__ == "__main__":
    main()

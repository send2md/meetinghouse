"""Meetinghouse's own duplicate-face check.

Whichever ID-check company checked a Member, Meetinghouse also compares
their verified selfie with every Member's, so nobody can join twice under
another name. It uses OpenCV's free face models from the OpenCV Model Zoo
(https://github.com/opencv/opencv_zoo): YuNet finds the face, and SFace
turns it into a "face print" of 128 numbers. Two face prints whose cosine
similarity is at least MATCH_THRESHOLD (SFace's published threshold) are
probably the same person. A match never blocks anyone by itself; it goes to
the Jury Committee, since twins and look-alikes exist.

We keep a face print only while its owner is a Member, plus while a Jury
Conversation about someone who left is open, or a Dismissal decided after
they left is still unserved (so leaving can't be used to dodge it).

    python3 facecheck.py fetch /path/to/data/folder

downloads the two models (about 39 MB) into data/models. The deploy does it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import urllib.request

MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
}
MATCH_THRESHOLD = 0.363


class FacePrinter:
    """Turns a photo into a face print, if OpenCV and the models are here."""

    def __init__(self, models_dir):
        self.detector = self.recognizer = None
        self.problem = ""
        try:
            import cv2  # optional: the site runs without it, and says so
        except ImportError:
            self.problem = "OpenCV isn't installed"
            return
        paths = [os.path.join(models_dir or "", name) for name in MODELS]
        if not all(os.path.isfile(p) for p in paths):
            self.problem = "the face models aren't downloaded"
            return
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(paths[0], "", (320, 320), 0.8)
        self.recognizer = cv2.FaceRecognizerSF.create(paths[1], "")

    def Available(self):
        return self.recognizer is not None

    def Print(self, image_bytes):
        """The face print of the largest face in the photo, or None if there's no face."""
        if not self.Available():
            return None
        import numpy as np

        image = self.cv2.imdecode(np.frombuffer(image_bytes, np.uint8), self.cv2.IMREAD_COLOR)
        if image is None:
            return None
        height, width = image.shape[:2]
        scale = min(1.0, 1024 / max(height, width))
        if scale < 1.0:
            image = self.cv2.resize(image, (int(width * scale), int(height * scale)))
            height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(image)
        if faces is None or len(faces) == 0:
            return None
        face = max(faces, key=lambda f: f[2] * f[3])
        aligned = self.recognizer.alignCrop(image, face)
        feature = self.recognizer.feature(aligned).flatten()
        return [float(round(x, 6)) for x in feature]


def Similarity(a, b):
    """Cosine similarity of two face prints: 1 is identical, around 0 unrelated."""
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def Matches(new_print, prints, threshold=MATCH_THRESHOLD):
    """[(participant id, similarity)] of the stored prints close to `new_print`, closest first."""
    found = [(pid, Similarity(new_print, p)) for pid, p in prints.items() if p]
    return sorted([m for m in found if m[1] >= threshold], key=lambda m: -m[1])


def Encode(face_print):
    return json.dumps(face_print, separators=(",", ":")) if face_print else ""


def Decode(text):
    return json.loads(text) if text else None


def Fetch(folder):
    models = os.path.join(folder, "models")
    os.makedirs(models, exist_ok=True)
    for name, url in MODELS.items():
        path = os.path.join(models, name)
        if os.path.isfile(path):
            continue
        with urllib.request.urlopen(url, timeout=300) as response:
            data = response.read()
        with open(path + ".new", "wb") as f:
            f.write(data)
        os.replace(path + ".new", path)
        print(f"{name}: {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}")
    return models


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "fetch":
        raise SystemExit(__doc__)
    Fetch(sys.argv[2])

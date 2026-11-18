"""Тесты монтажа с настоящим FFmpeg: стили, хук, водяной знак, кроп по лицу, раскадровка.
Пропускаются, если FFmpeg не установлен."""
import os
import shutil
import subprocess
import tempfile
import unittest

from shorts import face, storyboard
from shorts.render import probe, render_clip
from shorts.subtitles import STYLES
from shorts.transcribe import fake_words

HERE = os.path.dirname(os.path.abspath(__file__))


@unittest.skipIf(not shutil.which("ffmpeg"), "нет FFmpeg")
class MediaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.video = os.path.join(cls.dir, "face.mp4")
        # лицо справа от центра на цветном фоне
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=1280x720:rate=25", "-loop", "1",
                        "-i", os.path.join(HERE, "data", "face.jpg"), "-f", "lavfi", "-i", "sine",
                        "-filter_complex", "[1:v]scale=420:420[f];[0:v][f]overlay=820:150",
                        "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        "-shortest", cls.video], check=True)
        cls.info = probe(cls.video)

    def render(self, name, **kw):
        out = os.path.join(self.dir, name + ".mp4")
        with tempfile.TemporaryDirectory() as work:
            render_clip(self.video, self.info, fake_words(0, 12), 1, 6, out, work, **kw)
        info = probe(out)
        self.assertEqual((info["width"], info["height"]), (720, 1280))
        self.assertAlmostEqual(info["duration"], 5, delta=0.3)
        return out

    def test_all_styles(self):
        for style in STYLES:
            with self.subTest(style=style):
                self.render(style, style=style, position="middle" if style == "minimal" else "bottom")

    def test_hook_and_watermark_without_subtitles(self):
        self.render("hook", subtitles=False, hook="Как я всё понял", watermark="@test_bot")

    @unittest.skipIf(not face.available(), "нет OpenCV")
    def test_face_crop(self):
        x = face.face_center_x(self.video, 1, 10)
        self.assertIsNotNone(x)
        self.assertGreater(x, 0.7)  # лицо справа (центр ~0.8)
        self.assertLess(x, 0.9)
        self.render("crop", layout="crop")
        self.render("crop_flip", layout="crop", unique="strong")

    @unittest.skipIf(not storyboard.available(), "нет OpenCV")
    def test_storyboard(self):
        out = os.path.join(self.dir, "board.jpg")
        times = storyboard.make_storyboard(self.video, self.info["duration"], out)
        self.assertEqual(len(times), 12)
        self.assertGreater(os.path.getsize(out), 10000)


if __name__ == "__main__":
    unittest.main()

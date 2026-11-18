"""Тесты ядра без интернета: python -m unittest discover tests"""
import unittest
from unittest import mock

from shorts import config, highlights
from shorts.subtitles import _group, build_ass
from shorts.transcribe import fake_transcript


class SubtitlesTest(unittest.TestCase):
    def setUp(self):
        self.words = fake_transcript(60)["words"]

    def test_groups_fit_one_line(self):
        for g in _group(self.words):
            self.assertLessEqual(len(g), 3)
            if len(g) > 1:
                self.assertLessEqual(sum(len(w["word"]) + 1 for w in g) - 1, 15)

    def test_ass_times_are_relative_to_clip(self):
        ass = build_ass(self.words, 10, 30, 720, 1280)
        lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertTrue(lines)
        self.assertTrue(lines[0].split(",")[1].startswith("0:00:0"))
        for line in lines:
            start, end = line.split(",")[1:3]
            self.assertLess(start, end)
            self.assertLessEqual(end, "0:00:20.00")

    def test_braces_are_escaped(self):
        ass = build_ass([{"word": "{\\b1}хак", "start": 0, "end": 1}], 0, 2, 720, 1280)
        self.assertNotIn("{\\b1}", ass)


class StubsTest(unittest.TestCase):
    def test_auto_without_speech_key(self):
        from shorts import pipeline
        with mock.patch.object(config, "GROQ_API_KEY", ""):
            with self.assertRaises(pipeline.ServiceNotReady):
                pipeline.make_shorts("нет_такого.mp4", "/tmp/out_stub")


class HighlightsTest(unittest.TestCase):
    def setUp(self):
        self.duration = 600
        self.transcript = fake_transcript(self.duration)

    def check(self, clips, n):
        self.assertTrue(0 < len(clips) <= n)
        for c in clips:
            self.assertGreaterEqual(c["start"], 0)
            self.assertLessEqual(c["end"], self.duration)
            self.assertLessEqual(c["end"] - c["start"], config.CLIP_MAX_SEC + 1)
        for a, b in zip(clips, clips[1:]):
            self.assertLessEqual(a["end"], b["start"] + 0.5)

    def test_fallback(self):
        self.check(highlights.find_highlights(self.transcript, 5, self.duration, use_llm=False), 5)

    def test_llm_answer_is_cleaned(self):
        answer = [
            {"start": 10, "end": 50, "title": "Хороший", "score": 9},
            {"start": 30, "end": 70, "title": "Пересекается", "score": 5},
            {"start": 100, "end": 400, "title": "Слишком длинный", "score": 8},
            {"start": 200, "end": 203, "title": "Слишком короткий", "score": 10},
            {"start": "мусор", "end": None},
        ]
        with mock.patch.object(highlights, "_ask_llm", return_value=answer):
            clips = highlights.find_highlights(self.transcript, 5, self.duration)
        self.check(clips, 5)
        titles = [c["title"] for c in clips]
        self.assertIn("Хороший", titles)
        self.assertNotIn("Пересекается", titles)
        self.assertNotIn("Слишком короткий", titles)

    def test_empty_llm_answer_uses_fallback(self):
        with mock.patch.object(highlights, "_ask_llm", return_value=[]):
            self.check(highlights.find_highlights(self.transcript, 3, self.duration), 3)


if __name__ == "__main__":
    unittest.main()

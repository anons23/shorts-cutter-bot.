"""Тесты разбора таймингов: python -m unittest discover tests"""
import unittest

from shorts.timecodes import fit_to_duration, fmt, has_timecodes, parse_ranges, to_seconds


class TimecodesTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(to_seconds("1:20"), 80)
        self.assertEqual(to_seconds("1:02:03"), 3723)
        self.assertEqual(to_seconds("95"), 95)
        self.assertEqual(fmt(3723), "1:02:03")
        self.assertEqual(fmt(80), "1:20")

    def test_many_separators(self):
        text = "0:45-1:20\n3:10 — 3:55, 1:02:30–1:03:15; 80-105"
        ranges, errors = parse_ranges(text)
        self.assertEqual(errors, [])
        self.assertEqual(ranges, [(45, 80), (190, 235), (3750, 3795), (80, 105)])

    def test_link_numbers_are_ignored(self):
        text = "https://youtu.be/abc?t=90-120 1:00-1:30"
        self.assertEqual(parse_ranges(text)[0], [(60, 90)])
        self.assertFalse(has_timecodes("https://youtu.be/abc?t=90-120"))
        self.assertFalse(has_timecodes("привет"))

    def test_errors(self):
        ranges, errors = parse_ranges("1:30-1:00, 1:70-2:00, 0:00-5:00, 2:00-2:30")
        self.assertEqual(ranges, [(120, 150)])
        self.assertEqual(len(errors), 3)

    def test_limit_and_duration(self):
        ranges, errors = parse_ranges(", ".join(f"{i * 20}-{i * 20 + 10}" for i in range(15)))
        self.assertEqual(len(ranges), 10)
        self.assertEqual(len(errors), 1)
        ok, errors = fit_to_duration([(10, 20), (55, 70), (100, 110)], 60)
        self.assertEqual(ok, [(10, 20), (55, 60)])
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()

import math
import unittest
from saba.motion import cubic_bezier, generate_path


class MotionTests(unittest.TestCase):
    def test_endpoints(self):
        points = generate_path((10, 20), (250, 300), seed=4)
        self.assertEqual((points[0].x, points[0].y), (10, 20))
        self.assertEqual((points[-1].x, points[-1].y), (250, 300))

    def test_reproducible(self):
        self.assertEqual(generate_path((0, 0), (200, 50), seed=123),
                         generate_path((0, 0), (200, 50), seed=123))

    def test_timestamps_increase(self):
        pts = generate_path((0, 0), (100, 100), duration=1, fps=30)
        self.assertTrue(all(a.t < b.t for a, b in zip(pts, pts[1:])))

    def test_curve_endpoints(self):
        p = ((0, 0), (10, 20), (20, 30), (40, 50))
        self.assertEqual(cubic_bezier(*p, 0), p[0])
        self.assertEqual(cubic_bezier(*p, 1), p[3])

    def test_zero_distance(self):
        self.assertEqual(len(generate_path((5, 5), (5, 5))), 1)

    def test_invalid_duration(self):
        with self.assertRaises(ValueError):
            generate_path((0, 0), (1, 1), duration=math.nan)


if __name__ == "__main__":
    unittest.main()

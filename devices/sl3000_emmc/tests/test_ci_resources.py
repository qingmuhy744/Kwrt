from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_resources import available_memory, build_jobs, KIB_PER_GIB


class ResourceTests(unittest.TestCase):
    def test_public_runner_uses_all_four_cpus(self):
        self.assertEqual(build_jobs(4, 14 * KIB_PER_GIB), 4)

    def test_cpu_count_caps_parallelism(self):
        self.assertEqual(build_jobs(2, 16 * KIB_PER_GIB), 2)

    def test_low_memory_reduces_parallelism(self):
        self.assertEqual(build_jobs(4, 7 * KIB_PER_GIB), 2)
        self.assertEqual(build_jobs(4, 4 * KIB_PER_GIB), 1)

    def test_memory_boundary_keeps_the_runner_reserve(self):
        self.assertEqual(build_jobs(4, 13 * KIB_PER_GIB), 4)
        self.assertEqual(build_jobs(4, 13 * KIB_PER_GIB - 1), 3)

    def test_never_selects_unlimited_make_jobs(self):
        for available in (0, KIB_PER_GIB, 2 * KIB_PER_GIB):
            self.assertEqual(build_jobs(4, available), 1)

    def test_invalid_measurements_are_rejected(self):
        for cpus, memory in ((0, KIB_PER_GIB), (-1, KIB_PER_GIB), (4, -1)):
            with self.subTest(cpus=cpus, memory=memory), self.assertRaises(ValueError):
                build_jobs(cpus, memory)

    def test_uses_available_memory_not_total_or_free(self):
        meminfo = "MemTotal: 16384000 kB\nMemFree: 512000 kB\nMemAvailable: 14680064 kB\n"
        self.assertEqual(available_memory(meminfo), 14 * KIB_PER_GIB)

    def test_missing_or_invalid_available_memory_is_rejected(self):
        for meminfo in ("", "MemTotal: 16384000 kB", "MemAvailable: 123 MB", "MemAvailable: bad kB"):
            with self.subTest(meminfo=meminfo), self.assertRaises(ValueError):
                available_memory(meminfo)

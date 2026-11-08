"""RIFE model negotiation regression for Cazy Studio's Full Run exporter."""
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "source"))
engine = sys.modules.setdefault("engine", types.ModuleType("engine"))
if not hasattr(engine, "MediaInfo"):
    engine.MediaInfo = type("MediaInfo", (object,), {})
import quality_engine as q


class RifeModelCompatibilityTests(unittest.TestCase):
    def _exe(self, folder):
        exe = Path(folder) / "RIFE" / "rife-ncnn-vulkan.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        return exe

    def _v4(self, exe, name, complete=True):
        model = exe.parent / "models" / name
        model.mkdir(parents=True)
        (model / "flownet.param").write_text("test")
        if complete:
            (model / "flownet.bin").write_bytes(b"test")
        return model

    def test_legacy_2x_omits_unsupported_numframe(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = self._exe(folder)
            cmd, count, model = q._rife_interpolation_command(
                exe, "input", "output", 12, 60, 120)
            self.assertNotIn("-n", cmd)
            self.assertNotIn("-m", cmd)
            self.assertEqual(count, 24)
            self.assertIn("legacy", model)

    def test_selects_v4_automatically_and_passes_exact_frame_count(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = self._exe(folder)
            model_path = self._v4(exe, "rife-v4.6")
            cmd, count, model = q._rife_interpolation_command(
                exe, "input", "output", 100, 60, 120)
            self.assertEqual(cmd[cmd.index("-m") + 1], str(model_path))
            self.assertEqual(cmd[cmd.index("-n") + 1], "200")
            self.assertEqual(count, 200)
            self.assertEqual(model, "rife-v4.6")

    def test_prefers_newest_version_not_lexicographic(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = self._exe(folder)
            self._v4(exe, "rife-v4.6")
            model_path = self._v4(exe, "rife-v4.10")
            cmd, _, _ = q._rife_interpolation_command(
                exe, "input", "output", 30, 24, 60)
            self.assertEqual(cmd[cmd.index("-m") + 1], str(model_path))

    def test_v4_accepts_non_double_rates(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = self._exe(folder)
            self._v4(exe, "rife-v4")
            cmd, count, _ = q._rife_interpolation_command(
                exe, "input", "output", 40, 60, 90)
            self.assertIn("-n", cmd)
            self.assertEqual(count, 60)

    def test_incomplete_v4_model_not_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = self._exe(folder)
            self._v4(exe, "rife-v4.6", complete=False)
            cmd, count, _ = q._rife_interpolation_command(
                exe, "input", "output", 10, 60, 120)
            self.assertNotIn("-m", cmd)
            self.assertNotIn("-n", cmd)
            self.assertEqual(count, 20)

    def test_legacy_non_double_rate_explains_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = self._exe(folder)
            with self.assertRaisesRegex(RuntimeError, "RIFE v4.*Install or repair"):
                q._rife_interpolation_command(
                    exe, "input", "output", 40, 60, 120.5)

    def test_still_uses_rife_for_full_run_workflow(self):
        from inspect import getsource
        source = getsource(q._run_ai_sequence)
        self.assertIn("_rife_interpolation_command(", source)
        self.assertIn("progress_total=target_count", source)


if __name__ == "__main__":
    unittest.main()

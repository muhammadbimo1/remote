import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class BuildBatchTests(unittest.TestCase):
    def test_keeps_server_and_lua_assets_in_separate_runtime_folders(self):
        build_script = (ROOT / "build.bat").read_text(encoding="utf-8")

        self.assertIn(
            'xcopy "%CD%\\static\\*" "%STAGE%\\remote\\server\\static\\"',
            build_script,
        )
        self.assertNotIn(
            'xcopy "%CD%\\static\\*" "%STAGE%\\remote\\static\\"',
            build_script,
        )
        self.assertIn(
            'if exist "%TARGET%\\static" rmdir /s /q "%TARGET%\\static"',
            build_script,
        )
        self.assertIn(
            'if exist "%TARGET%\\server\\static" rmdir /s /q "%TARGET%\\server\\static"',
            build_script,
        )
        for asset in ("logo.png", "stinger.png", "Michroma-Regular.ttf"):
            self.assertTrue((ROOT / asset).is_file(), asset)
            self.assertFalse((ROOT / "static" / asset).exists(), asset)
            self.assertIn(
                f'copy /y "%CD%\\{asset}" "%STAGE%\\remote\\{asset}"',
                build_script,
            )

    def test_help_describes_repeatable_distribution_build(self):
        result = subprocess.run(
            f'"{ROOT / "build.bat"}" --help',
            cwd=ROOT,
            capture_output=True,
            text=True,
            shell=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Builds Broadcaster Remote into dist\\remote", result.stdout)
        self.assertIn("preserves dist\\remote\\server\\remote_config.json", result.stdout)

    def test_includes_engineio_threading_backend_used_at_runtime(self):
        build_script = (ROOT / "build.bat").read_text(encoding="utf-8")

        self.assertIn(
            "--hidden-import engineio.async_drivers.threading",
            build_script,
        )

    def test_publishes_without_renaming_the_distribution_directory(self):
        build_script = (ROOT / "build.bat").read_text(encoding="utf-8")

        self.assertNotIn('move "%TARGET%"', build_script)
        self.assertIn(
            'xcopy "%STAGE%\\remote\\*" "%TARGET%\\" /e /i /q /y',
            build_script,
        )

    def test_does_not_rewrite_tracked_python_bytecode(self):
        build_script = (ROOT / "build.bat").read_text(encoding="utf-8")

        self.assertIn('set "PYTHONDONTWRITEBYTECODE=1"', build_script)


if __name__ == "__main__":
    unittest.main()

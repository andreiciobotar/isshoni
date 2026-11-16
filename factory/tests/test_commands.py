"""@command: a command declared beside its function is parsed, listed in order and run by main."""
import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_factory import entry, factory


def listed_commands():
    """The names of the top-level commands, in the order `factory --help` lists them."""
    with mock.patch.object(argparse.ArgumentParser, "parse_args", lambda self, argv: self):
        root = factory.parse_args([])
    sub = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    return list(sub.choices)


class CommandTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(factory.COMMANDS)  # restores COMMANDS, whatever a test declares
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_declared_command_is_parsed_and_run_by_main(self):
        def args_probe(parser):
            parser.add_argument("--times", type=int, default=1)

        @factory.command("probe", "a throwaway", args_probe)
        def cmd_probe(args):
            return 40 + args.times

        self.assertEqual(factory.main(["probe", "--times", "2"]), 42)
        self.assertEqual(factory.main(["probe"]), 41)

    def test_declared_command_without_options(self):
        @factory.command("probe", "a throwaway")
        def cmd_probe(args):
            return 7

        self.assertEqual(factory.main(["probe"]), 7)

    def test_listing_is_the_order_line_then_extra_names_sorted(self):
        with mock.patch.object(factory, "COMMAND_ORDER", ("status", "new")), \
                mock.patch.dict(factory.COMMANDS, clear=True):
            for name in ("new", "status"):
                factory.command(name, "ordered")(lambda args: 0)
            self.assertEqual(listed_commands(), ["status", "new"])
            factory.command("zz-b", "extra")(lambda args: 0)
            self.assertEqual(listed_commands(), ["status", "new", "zz-b"])
            factory.command("aa-c", "extra")(lambda args: 0)
            self.assertEqual(listed_commands(), ["status", "new", "aa-c", "zz-b"])

    def test_close_is_listed_right_after_ack(self):
        names = listed_commands()
        self.assertEqual(names[names.index("ack") + 1], "close")

    def test_the_order_line_names_only_declared_commands(self):
        self.assertLessEqual(set(factory.COMMAND_ORDER), set(factory.COMMANDS))

    def test_a_modules_commands_are_declared_by_importing_it(self):
        for name in ("wizard", "uninstall"):
            self.assertEqual(factory.COMMANDS[name][2].__module__, "factory_setup")
        self.assertIs(factory.COMMANDS["wizard"][2], entry.factory_setup.cmd_wizard)

    def test_the_core_does_not_name_a_module(self):
        self.assertNotIn("factory_setup", Path(factory.__file__).read_text())

    def test_the_entry_point_finds_its_modules_through_a_symlink_from_another_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "factory"
            link.symlink_to(factory.FACTORY)
            run = subprocess.run([sys.executable, str(link), "--help"], cwd=tmp, capture_output=True, text=True,
                                 env={**os.environ, "FACTORY_HOME": tmp})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("wizard", run.stdout)


if __name__ == "__main__":
    unittest.main()

"""@command: a command declared beside its function is parsed, listed in order and run by main."""
import argparse
import unittest
from unittest import mock

from test_factory import factory


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

    def test_the_order_line_names_only_declared_commands(self):
        self.assertLessEqual(set(factory.COMMAND_ORDER), set(factory.COMMANDS))


if __name__ == "__main__":
    unittest.main()

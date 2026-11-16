"""Print the whole command-line surface of a `factory` script: for every parser and subparser, its help text
and one line per argument. To compare two revisions, run it on each and diff:

    python3 tools/cli_snapshot.py <old>/factory/bin/factory > old.txt
    python3 tools/cli_snapshot.py factory/bin/factory > new.txt
    diff old.txt new.txt

Knows nothing of how the script declares its commands, so it works on any revision. Usage: cli_snapshot.py [script]."""
import argparse
import importlib.machinery
import importlib.util
import os
import sys

os.environ["COLUMNS"] = "100"  # the width argparse wraps help at; set before any parser exists
script = sys.argv[1] if len(sys.argv) > 1 else "factory/bin/factory"
loader = importlib.machinery.SourceFileLoader("factory_bin", script)
module = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_bin", loader))
loader.exec_module(module)

roots = []
real_parse_args = argparse.ArgumentParser.parse_args
argparse.ArgumentParser.parse_args = lambda self, *a, **k: roots.append(self) or argparse.Namespace()
module.parse_args([])
argparse.ArgumentParser.parse_args = real_parse_args


def show(parser, path):
    print(f"=== {path}")
    print(parser.format_help())
    for a in parser._actions:
        kind = getattr(a.type, "__name__", a.type)
        print(f"{type(a).__name__} {a.option_strings} dest={a.dest} nargs={a.nargs} const={a.const!r} default={a.default!r} type={kind} "
              f"choices={list(a.choices) if a.choices is not None else None} required={a.required} metavar={a.metavar!r} "
              f"help={a.help!r}")
    for g in parser._mutually_exclusive_groups:
        print(f"exclusive required={g.required}: {[a.dest for a in g._group_actions]}")
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for name, child in a.choices.items():
                show(child, f"{path} {name}")


show(roots[0], "factory")

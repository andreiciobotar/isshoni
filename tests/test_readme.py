"""README.md names only things that exist: make targets, subcommands, skills, paths and anchors."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRAMS = {"isshoni": "bin/isshoni", "factory": "factory/bin/factory"}
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\]\(([^)\s]+)\)|\b(?:src|srcset)=\"([^\"]+)\"")
CODE_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+")
SUBCOMMAND = re.compile(r"(?<![\w.-])(isshoni|factory)[ \t]+([A-Za-z][\w-]*)")


def slug(heading):
    return re.sub(r"[^\w\s-]", "", heading.lower()).replace(" ", "-")


def extract(text):
    """What the README names, by kind: {kind: sorted set of items}."""
    fences = FENCE.findall(text)
    code = "\n".join(fences + SPAN.findall(FENCE.sub("", text)))
    found = {
        "make": set(re.findall(r"\bmake ([\w-]+)", code)),
        "subcommand": {m.groups() for m in SUBCOMMAND.finditer(code)},
        "skill": set(re.findall(r"/factory:([\w-]+)", code)),
        "link": set(),
        "anchor": set(),
        "path": {t for t in code.split() if CODE_PATH.fullmatch(t)},
    }
    for target in (g for m in LINK.finditer(text) for g in m.groups() if g):
        if re.match(r"(https?:|mailto:|#)", target):
            continue
        path, _, anchor = target.partition("#")
        found["link"].add(path)
        if anchor and path.endswith(".md"):
            found["anchor"].add((path, anchor))
    return found


def exists(path):
    return os.path.exists(os.path.join(ROOT, path))


def understood(program, word):
    try:
        done = subprocess.run([os.path.join(ROOT, PROGRAMS[program]), word, "--help"],
                              capture_output=True, timeout=30, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def has_anchor(path, anchor):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        text = FENCE.sub("", f.read())
    return anchor in {slug(m.group(1)) for m in re.finditer(r"^#+\s+(.*?)\s*$", text, re.M)}


def problems(text):
    """One line per name in `text` (a README) that does not exist."""
    found = extract(text)
    with open(os.path.join(ROOT, "Makefile"), encoding="utf-8") as f:
        targets = set(re.findall(r"^([\w-]+):", f.read(), re.M))
    out = [f"make target {n}" for n in sorted(found["make"] - targets)]
    out += [f"subcommand {p} {w}" for p, w in sorted(found["subcommand"]) if not understood(p, w)]
    out += [f"skill /factory:{n}" for n in sorted(found["skill"])
            if not exists(f"factory/skills/{n}/SKILL.md")]
    out += [f"link path {p}" for p in sorted(found["link"]) if not exists(p)]
    out += [f"anchor {p}#{a}" for p, a in sorted(found["anchor"])
            if exists(p) and not has_anchor(p, a)]
    out += [f"path in code {p}" for p in sorted(found["path"]) if not exists(p)]
    return out


class ReadmeNamesExistingThings(unittest.TestCase):
    def test_readme_has_no_problems(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            self.assertEqual(problems(f.read()), [])

    def test_each_kind_of_bad_name_is_found(self):
        text = """
Run `make test` and `make nosuchtarget`, `isshoni sessions` and `isshoni nosuchword`,
`bin/isshoni agents`, `isshoni-tui --serve`, `/factory:dispatch` and `/factory:nosuchskill`.

```sh
factory new --help
factory nosuchword
ls docs/factory.md docs/nosuchfile.md
```

[good](docs/factory.md#settings) [bad file](docs/nosuchfile.md) [bad anchor](docs/factory.md#no-such-heading)
<img src="assets/nosuch.png" srcset="README.md"> [web](https://example.com/x/y) [top](#top)
"""
        self.assertEqual(problems(text), [
            "make target nosuchtarget",
            "subcommand factory nosuchword",
            "subcommand isshoni nosuchword",
            "skill /factory:nosuchskill",
            "link path assets/nosuch.png",
            "link path docs/nosuchfile.md",
            "anchor docs/factory.md#no-such-heading",
            "path in code docs/nosuchfile.md",
        ])

    def test_readme_names_each_kind_at_least_once(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            found = extract(f.read())
        for kind in ("make", "subcommand", "link", "anchor"):
            self.assertTrue(found[kind], kind)
        self.assertTrue(any(p == "isshoni" for p, _ in found["subcommand"]))


if __name__ == "__main__":
    unittest.main()

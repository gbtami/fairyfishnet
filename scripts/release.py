#!/usr/bin/env python3
"""Create, tag, and publish a fairyfishnet release."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*command):
    print("$", " ".join(command))
    subprocess.run(command, cwd=str(ROOT), check=True)


def check_git():
    print("--- CHECK GIT --------------------------------------------------------")
    run("git", "diff", "--exit-code")
    run("git", "diff", "--cached", "--exit-code")


def check_project():
    print("--- CHECK PROJECT ----------------------------------------------------")
    run("uv", "run", "pytest")
    run("uv", "run", "ruff", "check", ".")
    run("uv", "run", "ruff", "format", "--check", ".")
    run("uv", "run", "pyright")
    run("uv", "lock", "--check", "--python", sys.executable)
    run("uv", "build")


def tag_and_push(release_version):
    print("--- TAG AND PUSH -----------------------------------------------------")
    tagname = "v%s" % release_version
    guessed_tagname = input(">>> Sure? Confirm tagname: ")
    if guessed_tagname != tagname:
        print("Actual tag name is: %s" % tagname)
        sys.exit(1)

    run("git", "tag", tagname)
    run("git", "push", "--atomic", "origin", "master", tagname)


def publish():
    print("--- PYPI -------------------------------------------------------------")
    run("uv", "publish")


def main():
    release_version = version("fairyfishnet")
    check_project()
    check_git()
    tag_and_push(release_version)
    publish()


if __name__ == "__main__":
    main()

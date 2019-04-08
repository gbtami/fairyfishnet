#!/usr/bin/env python
# Helper script to create and publish a new fishnet release.

from __future__ import print_function

import os
import sys
import fishnet

try:
    input = raw_input
except NameError:
    pass


def system(command):
    print(command)
    exit_code = os.system(command)
    if exit_code != 0:
        sys.exit(exit_code)


def check_git():
    print("--- CHECK GIT --------------------------------------------------------")
    system("git diff --exit-code")
    system("git diff --cached --exit-code")


def test():
    print("--- TEST -------------------------------------------------------------")
    system("python2 test.py")
    system("python3 test.py")


def check_docs():
    print("--- CHECK DOCS -------------------------------------------------------")
    system("python3 setup.py --long-description | rst2html --strict --no-raw > /dev/null")


def tag_and_push():
    print("--- TAG AND PUSH -------------------------------")
    tagname = "v{0}".format(fishnet.__version__)
    guessed_tagname = input(">>> Sure? Confirm tagname: ")
    if guessed_tagname != tagname:
        print("Actual tag name is: {0}".format(tagname))
        sys.exit(1)

    system("git tag {0}".format(tagname))
    system("git push --atomic origin master {0}".format(tagname))


def pypi():
    print("--- PYPI -------------------------------------------------------------")
    system("rm -rf build")
    system("python3 setup.py sdist bdist_wheel --universal")
    system("twine check dist/*")
    system("twine upload --skip-existing --sign dist/*")


if __name__ == "__main__":
    test()
    check_docs()
    check_git()
    tag_and_push()
    pypi()

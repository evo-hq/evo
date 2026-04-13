"""Tiny math solver. Intentionally naive — only handles addition of two positive integers."""

import re


def solve(question: str) -> str:
    """Return the answer to a math question as a string.

    Current behaviour: extracts two integers from the question and adds them.
    Returns "unknown" if the question doesn't match the expected pattern.
    """
    match = re.findall(r"\d+", question)
    if len(match) == 2:
        return str(int(match[0]) + int(match[1]))
    return "unknown"


if __name__ == "__main__":
    import sys
    print(solve(" ".join(sys.argv[1:])))

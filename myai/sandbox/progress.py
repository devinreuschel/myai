import sys
from typing import TextIO


class RunProgress:
    def __init__(self, *, quiet: bool = False, stream: TextIO = sys.stderr) -> None:
        self.quiet = quiet
        self.stream = stream

    def say(self, message: str) -> None:
        if not self.quiet:
            print(message, file=self.stream, flush=True)

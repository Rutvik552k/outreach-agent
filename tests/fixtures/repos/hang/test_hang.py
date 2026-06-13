import time


def test_hangs_forever():
    # F-10: a non-terminating suite. DockerSandboxRunner must kill the container
    # at the wall-clock timeout and return verdict == timeout, NOT block the
    # serialized pipeline indefinitely. Kept as a busy-ish sleep loop so it never
    # returns on its own within any realistic timeout.
    while True:
        time.sleep(3600)

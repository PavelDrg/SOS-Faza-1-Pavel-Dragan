"""Mesaje pe stdout."""


def log(message: str) -> None:
    # flush imediat: la testele de repornire procesul poate fi oprit oricând
    print(message, flush=True)

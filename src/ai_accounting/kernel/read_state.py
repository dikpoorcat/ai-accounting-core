"""Read-model repairs have their own revision, separate from business epochs."""

from .contracts import KernelError


def repair_revision(connection):
    return connection.execute("SELECT read_repair_revision FROM state WHERE id=1").fetchone()[0]


def check_repair_revision(connection, expected):
    if repair_revision(connection) != expected:
        raise KernelError("preview_expired", "读取依据已经修复，请重新预览")


def advance_repair_revision(connection):
    connection.execute("UPDATE state SET read_repair_revision=read_repair_revision+1 WHERE id=1")
    return repair_revision(connection)

"""Short, serialized SQLite mutations; callers own commit/rollback.

Take the write reservation before reading optimistic versions or allocating
sequence numbers. Do filesystem/network probes before entering this boundary.
"""

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction


@event.listens_for(Session, "after_transaction_end")
def clear_write_marker(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop("domain_write", None)


def begin_write(session: Session) -> None:
    if not session.info.get("domain_write"):
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session.info["domain_write"] = True

"""Connection-pool configuration for the request-path engine.

A pooled connection can die without either end noticing -- Cloud Run reaches
Cloud SQL through a VPC connector whose instances are recycled as it scales.
Without validation on checkout, the dead connection is handed to a request and
the first statement of the transaction raises
``InterfaceError: connection is closed`` as a 500. Postgres logs nothing, because
Postgres did not close it.
"""

from app.infrastructure.db.engine import engine


def test_request_engine_validates_connections_on_checkout() -> None:
    """Pre-ping is the guard that turns a dead connection into a silent retry."""
    assert engine.pool._pre_ping is True


def test_request_engine_retires_connections_before_they_go_stale() -> None:
    """SQLAlchemy's default is -1 (never recycle), which is what let sockets rot."""
    recycle = engine.pool._recycle
    assert recycle > 0
    # Comfortably under the idle windows of the network path in between.
    assert recycle <= 3600

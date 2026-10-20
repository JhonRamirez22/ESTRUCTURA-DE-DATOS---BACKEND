from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import customer_notifications
from app.settings import Settings


def test_customer_notifications_are_off_by_default() -> None:
    assert Settings.model_fields["customer_notifications_enabled"].default is False


@pytest.mark.asyncio
async def test_nothing_is_queued_while_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        customer_notifications,
        "get_settings",
        lambda: Settings(_env_file=None, customer_notifications_enabled=False),  # type: ignore[call-arg]
    )
    point = SimpleNamespace(
        customer_email="cliente@example.com", email_notifications_enabled=True,
        customer_phone="3001234567", sms_notifications_enabled=True,
    )
    # La sesión es None a propósito: con el interruptor apagado ni se consulta.
    assigned = await customer_notifications.enqueue_order_assigned_notification(
        None, point, uuid4()  # type: ignore[arg-type]
    )
    near = await customer_notifications.enqueue_courier_near_notification(
        None, point, uuid4()  # type: ignore[arg-type]
    )
    assert assigned == () and near == ()

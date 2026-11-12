"""Fixtures shared by the unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def allow_any_image(monkeypatch):
    """Admit every image a pull pod names, for tests of what the pod does after admission."""
    from services.integration.image_policy import ImagePolicy
    from services.orchestration.gates import image_admission

    async def admit_images(images):
        return image_admission.Admission(images=(), policy=ImagePolicy(), tenant="")

    monkeypatch.setattr(image_admission, "admit_images", admit_images)
    monkeypatch.setattr(image_admission, "pod_service_account", lambda: "")

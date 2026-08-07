from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.admin import build_admin_router


class FakeConfigManager:
    def __init__(self):
        self.data = {
            "leonardo_credit_price_cny": None,
            "adobe_credit_price_cny": None,
            "generated_max_size_mb": 1024,
            "generated_prune_size_mb": 200,
        }

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def get_all(self) -> dict:
        return dict(self.data)

    def update_all(self, values: dict) -> None:
        self.data.update(values)


def make_client(config: FakeConfigManager) -> TestClient:
    api = FastAPI()
    api.include_router(
        build_admin_router(
            static_dir=Path("."),
            token_manager=object(),
            config_manager=config,
            refresh_manager=object(),
            log_store=object(),
            error_store=object(),
            live_log_store=object(),
            require_admin_auth=lambda request: None,
            is_admin_authenticated=lambda request: True,
            apply_client_config=lambda: None,
            get_generated_storage_stats=lambda: {},
        )
    )
    return TestClient(api)


def test_admin_saves_two_provider_prices_and_can_clear_one():
    config = FakeConfigManager()
    client = make_client(config)

    response = client.put(
        "/api/v1/config",
        json={
            "leonardo_credit_price_cny": 0.001,
            "adobe_credit_price_cny": 0.002,
        },
    )
    assert response.status_code == 200
    assert config.data["leonardo_credit_price_cny"] == 0.001
    assert config.data["adobe_credit_price_cny"] == 0.002

    cleared = client.put(
        "/api/v1/config", json={"leonardo_credit_price_cny": None}
    )
    assert cleared.status_code == 200
    assert config.data["leonardo_credit_price_cny"] is None
    assert config.data["adobe_credit_price_cny"] == 0.002


def test_admin_rejects_invalid_provider_price_without_mutation():
    config = FakeConfigManager()

    response = make_client(config).put(
        "/api/v1/config", json={"adobe_credit_price_cny": 0.0000001}
    )

    assert response.status_code == 422
    assert config.data["adobe_credit_price_cny"] is None

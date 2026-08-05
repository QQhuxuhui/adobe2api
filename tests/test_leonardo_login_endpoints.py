import pytest
from pydantic import ValidationError
from api.schemas import LeonardoLoginReportRequest


def test_report_model_requires_error_on_failure():
    with pytest.raises(ValidationError):
        LeonardoLoginReportRequest(id="a", credential_rev=1, status="login_required")


def test_report_model_forbids_error_on_ok():
    with pytest.raises(ValidationError):
        LeonardoLoginReportRequest(id="a", credential_rev=1, status="ok", last_error_kind="password")


def test_report_model_rejects_negative_balance():
    with pytest.raises(ValidationError):
        LeonardoLoginReportRequest(id="a", credential_rev=1, status="ok", balance=-1)


def test_report_model_ok():
    m = LeonardoLoginReportRequest(id="a", credential_rev=2, status="login_required",
                                   last_error_kind="captcha", balance=10.5)
    assert m.balance == 10.5

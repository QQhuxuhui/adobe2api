from core.mail_otp import extract_canva_otp


def test_extract_otp_chinese_subject():
    assert extract_canva_otp("你的Canva可画验证码是100581") == "100581"


def test_extract_otp_english():
    assert extract_canva_otp("Your Canva verification code is 482913") == "482913"


def test_extract_otp_indonesian():
    assert extract_canva_otp("Kode Canva anda adalah 123456") == "123456"


def test_extract_otp_generic_code_colon():
    assert extract_canva_otp("Canva\nYour code: 654321") == "654321"


def test_extract_otp_none_when_no_code():
    assert extract_canva_otp("Welcome to Canva, let's get started") is None


def test_extract_otp_ignores_unrelated_numbers():
    # 无 canva/code/验证码 语境的 6 位数字不应误命中
    assert extract_canva_otp("Order #123456 has shipped") is None

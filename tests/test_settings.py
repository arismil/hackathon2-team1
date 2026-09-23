from hackathon2_team1.settings import Settings


def test_settings_use_a_safe_local_default() -> None:
    assert Settings.from_environment({}).environment == "local"


def test_settings_read_the_named_environment_variable() -> None:
    assert Settings.from_environment({"APP_ENV": "demo"}).environment == "demo"

"""``redact_secret_values`` removes every secrets value from the lines it is given."""

from __future__ import annotations

from esphome_device_builder.helpers.secret_redaction import redact_secret_values


def test_redacts_every_scalar_of_credential_length() -> None:
    mappings = [
        {"user": "alice_smith", "port": 1883, "flag": True, "nothing": None},
        {
            "user": "qwertyui",
            "nested": [{"token": "abcdefgh"}],
            "cert": "FIRSTLINEOFCERT\nSECONDLINE\n",
        },
    ]
    lines = [
        "user: alice_smith and qwertyui",
        "port: 1883 flag: True",
        "token: abcdefgh",
        "SECONDLINE",
    ]
    assert redact_secret_values(lines, mappings) == [
        "user: <removed> and <removed>",
        "port: 1883 flag: True",
        "token: <removed>",
        "<removed>",
    ]


def test_a_longer_value_is_removed_before_its_prefix() -> None:
    mappings = [{"a": "hunter2", "b": "hunter2-extended"}]
    assert redact_secret_values(["x hunter2-extended y"], mappings) == ["x <removed> y"]


def test_equal_length_values_are_removed_in_a_stable_order() -> None:
    mappings = [{"a": "abcdef", "b": "defghi"}]
    assert redact_secret_values(["abcdefghi"], mappings) == ["<removed>ghi"]


def test_a_blank_value_is_not_a_redaction_target() -> None:
    lines = ["sensor:", "        - platform: dht"]
    assert redact_secret_values(lines, [{"padding": "        "}]) == lines

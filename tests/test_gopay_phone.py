import unittest

from services.chatgpt_core.gopay_phone import (
    normalize_gopay_recognized_country_codes,
    split_gopay_phone_input,
)


class GoPayPhoneTests(unittest.TestCase):
    def test_default_recognized_code_splits_62(self):
        phone = split_gopay_phone_input("62", "628123456789", ["62"])
        self.assertEqual(phone, {"phone_country_code": "62", "phone_number": "8123456789"})

    def test_unrecognized_prefix_keeps_number_and_default_country(self):
        phone = split_gopay_phone_input("62", "8613812345678", ["62"])
        self.assertEqual(phone, {"phone_country_code": "62", "phone_number": "8613812345678"})

    def test_configured_code_splits_only_after_added(self):
        phone = split_gopay_phone_input("62", "8613812345678", ["62", "86"])
        self.assertEqual(phone, {"phone_country_code": "86", "phone_number": "13812345678"})

    def test_local_zero_prefix_is_preserved(self):
        phone = split_gopay_phone_input("62", "08123456789", ["62"])
        self.assertEqual(phone, {"phone_country_code": "62", "phone_number": "08123456789"})

    def test_plus_prefix_is_supported(self):
        phone = split_gopay_phone_input("", "+628123456789", ["62"])
        self.assertEqual(phone, {"phone_country_code": "62", "phone_number": "8123456789"})

    def test_longest_country_code_wins(self):
        phone = split_gopay_phone_input("1", "12425550123", ["1", "1242"])
        self.assertEqual(phone, {"phone_country_code": "1242", "phone_number": "5550123"})

    def test_empty_recognized_list_falls_back_to_62(self):
        self.assertEqual(normalize_gopay_recognized_country_codes([]), ["62"])


if __name__ == "__main__":
    unittest.main()

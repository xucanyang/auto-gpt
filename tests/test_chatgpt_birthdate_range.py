import unittest
from datetime import datetime

from services.chatgpt_core.constants import (
    MAX_REGISTRATION_AGE,
    MIN_REGISTRATION_AGE,
    generate_random_user_info,
)
from services.chatgpt_core.utils import generate_random_birthday


class ChatGPTBirthdateRangeTests(unittest.TestCase):
    def test_generate_random_user_info_birthdate_stays_within_20_to_45(self):
        current_year = datetime.now().year

        for _ in range(50):
            birthdate = generate_random_user_info()["birthdate"]
            birth_year = int(birthdate.split("-", 1)[0])
            age = current_year - birth_year
            self.assertGreaterEqual(age, MIN_REGISTRATION_AGE)
            self.assertLessEqual(age, MAX_REGISTRATION_AGE)

    def test_generate_random_birthday_stays_within_20_to_45(self):
        current_year = datetime.now().year

        for _ in range(50):
            birthdate = generate_random_birthday()
            birth_year = int(birthdate.split("-", 1)[0])
            age = current_year - birth_year
            self.assertGreaterEqual(age, MIN_REGISTRATION_AGE)
            self.assertLessEqual(age, MAX_REGISTRATION_AGE)


if __name__ == "__main__":
    unittest.main()

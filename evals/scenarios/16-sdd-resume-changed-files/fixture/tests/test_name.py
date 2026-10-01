import unittest
from src.name import format_name
class NameTest(unittest.TestCase):
    def test_upper(self):
        self.assertEqual(format_name("sample"), "SAMPLE")
if __name__ == "__main__":
    unittest.main()

import os
import unittest
import sys
from pathlib import Path

# Add the directory containing selfmod.py to sys.path
sys.path.append(str(Path(__file__).parent.resolve()))

from selfmod import SelfModEngine

class TestASTWeaver(unittest.TestCase):
    def setUp(self):
        # Mocking the functions required by SelfModEngine constructor
        self.engine = SelfModEngine(
            get_tools_fn=lambda: [],
            get_handler_fn=lambda: None,
            register_tool_fn=lambda t, fn: None
        )
        self.test_file = "test_target.py"
        with open(self.test_file, "w") as f:
            f.write("def old_func():\n    # original comment\n    return 'old'\n")

    def tearDown(self):
        if os.path.exists(self.test_file):
            os.remove(self.test_file)

    def test_ast_replace_function(self):
        new_code = "def old_func():\n    return 'new'\n"
        result = self.engine.ast_replace_function(self.test_file, "old_func", new_code)
        self.assertTrue(result["success"])
        with open(self.test_file, "r") as f:
            content = f.read()
            self.assertIn("'new'", content)
            self.assertNotIn("'old'", content)

if __name__ == "__main__":
    unittest.main()

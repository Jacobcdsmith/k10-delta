import os
import sys
from pathlib import Path

# Add the directory containing selfmod.py to sys.path
sys.path.append(str(Path(__file__).parent.resolve()))

from selfmod import SelfModEngine

def test_formatting_preservation():
    engine = SelfModEngine(
        get_tools_fn=lambda: [],
        get_handler_fn=lambda: None,
        register_tool_fn=lambda t, fn: None
    )
    test_file = "test_formatting_target.py"
    
    original_content = """# Top comment
def other_func():
    pass

def target_func():
    # Inside comment
    return 1

# Bottom comment
"""
    with open(test_file, "w") as f:
        f.write(original_content)

    try:
        new_code = "def target_func():\n    return 'replaced'\n"
        result = engine.ast_replace_function(test_file, "target_func", new_code)
        
        if not result["success"]:
            print(f"FAILED: {result.get('error')}")
            sys.exit(1)

        with open(test_file, "r") as f:
            content = f.read()
            
        print("--- Updated Content ---")
        print(content)
        print("-----------------------")

        # Assertions
        assert "# Top comment" in content
        assert "def other_func():" in content
        assert "pass" in content
        assert "# Bottom comment" in content
        assert "return 'replaced'" in content
        assert "return 1" not in content
        assert "# Inside comment" not in content # The whole function was replaced

        print("SUCCESS: Formatting and surrounding comments preserved!")

    finally:
        if os.path.exists(test_file):
            os.remove(test_file)

if __name__ == "__main__":
    test_formatting_preservation()

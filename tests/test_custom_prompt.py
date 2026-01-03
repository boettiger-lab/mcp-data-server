#!/usr/bin/env python3
"""
Test script to verify custom prompt functionality.
"""
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

def test_prompt_loading():
    """Test that prompt loading works correctly."""
    # Import the prompt module directly to avoid dependency issues
    from mcp_data_server.prompt import get_full_prompt
    
    print("Testing prompt loading functionality...")
    print("-" * 60)
    
    # Test 1: Load default prompt
    print("\n1. Testing default prompt (built-in wetlands-data.md):")
    default_prompt = get_full_prompt()
    print(f"   ✓ Default prompt length: {len(default_prompt):,} characters")
    assert len(default_prompt) > 1000, "Default prompt should be substantial"
    assert "Wetlands Data Context" in default_prompt, "Should contain wetlands context"
    assert "DuckDB" in default_prompt, "Should contain DuckDB context"
    print(f"   ✓ Contains expected content")
    
    # Test 2: Load custom prompt
    print("\n2. Testing custom prompt (wetlands-data.md from workspace root):")
    custom_path = Path(__file__).parent / "wetlands-data.md"
    if custom_path.exists():
        custom_prompt = get_full_prompt(str(custom_path))
        print(f"   ✓ Custom prompt length: {len(custom_prompt):,} characters")
        assert len(custom_prompt) > 1000, "Custom prompt should be substantial"
        assert "Wetlands Data Context" in custom_prompt, "Should contain wetlands context"
        print(f"   ✓ Successfully loaded from: {custom_path}")
    else:
        print(f"   ⚠ Skipping test - file not found: {custom_path}")
    
    # Test 3: Verify error handling for non-existent file
    print("\n3. Testing error handling for non-existent file:")
    try:
        get_full_prompt("/nonexistent/path/to/prompt.md")
        print("   ✗ Should have raised FileNotFoundError")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"   ✓ Correctly raised FileNotFoundError: {e}")
    
    print("\n" + "-" * 60)
    print("✅ All tests passed!")
    print("\nThe custom prompt feature is working correctly.")
    print("You can now use --custom-prompt flag when launching the server.")

if __name__ == "__main__":
    try:
        test_prompt_loading()
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

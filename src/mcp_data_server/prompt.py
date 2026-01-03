"""
Prompt loading utilities for the wetlands MCP server.
"""
from pathlib import Path

def load_prompt_from_file(filename: str) -> str:
    """Load prompt content from a markdown file."""
    prompt_dir = Path(__file__).parent
    prompt_path = prompt_dir / filename
    
    with open(prompt_path, 'r', encoding='utf-8') as f:
        return f.read()

def get_full_prompt(custom_prompt_path: str | None = None) -> str:
    """Get the complete system prompt by combining contexts.
    
    Args:
        custom_prompt_path: Optional path to a custom prompt markdown file.
    
    Returns:
        Combined prompt text from custom context and DuckDB context.
    """
    # Load custom prompt if provided, otherwise use built-in wetlands context
    if custom_prompt_path:
        custom_path = Path(custom_prompt_path)
        if not custom_path.exists():
            raise FileNotFoundError(f"Custom prompt file not found: {custom_prompt_path}")
        with open(custom_path, 'r', encoding='utf-8') as f:
            context = f.read()
    else:
        context = load_prompt_from_file('wetlands-data.md')
    
    duckdb_context = load_prompt_from_file('duckdb-prompt.md')
    
    return f"{context}\n\n{duckdb_context}"

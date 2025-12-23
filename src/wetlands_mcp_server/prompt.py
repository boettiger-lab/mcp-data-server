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

def get_full_prompt() -> str:
    """Get the complete system prompt by combining wetlands data and DuckDB contexts."""
    wetlands_context = load_prompt_from_file('wetlands-data.md')
    duckdb_context = load_prompt_from_file('duckdb-prompt.md')
    
    return f"{wetlands_context}\n\n{duckdb_context}"

# For backwards compatibility, export PROMPT_TEMPLATE
PROMPT_TEMPLATE = get_full_prompt()

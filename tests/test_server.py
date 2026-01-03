#!/usr/bin/env python3
"""
Test script for the Wetlands MCP Server.
Tests simple queries to the datasets using the external endpoint.
"""

import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_query(session: ClientSession, query: str, description: str) -> bool:
    """
    Test a SQL query against the MCP server.
    
    Args:
        session: MCP client session
        query: SQL query to execute
        description: Description of what the query tests
    
    Returns:
        True if the query succeeded, False otherwise
    """
    print(f"\n{'='*60}")
    print(f"Test: {description}")
    print(f"{'='*60}")
    print(f"Query preview: {query[:100]}...")
    
    try:
        result = await session.call_tool("query", arguments={"query": query})
        
        if result.content and len(result.content) > 0:
            text = result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])
            
            # Check if the result contains an error message
            if "Error executing" in text or "❌" in text:
                print(f"✗ FAILED")
                lines = text.split("\n")
                preview_lines = lines[:5]
                print("Error output:")
                for line in preview_lines:
                    print(f"  {line}")
                return False
            
            print(f"✓ SUCCESS")
            lines = text.split("\n")
            preview_lines = lines[:5]
            print("Result preview:")
            for line in preview_lines:
                print(f"  {line}")
            if len(lines) > 5:
                remaining = len(lines) - 5
                print(f"  ... ({remaining} more lines)")
        return True
    except Exception as e:
        print(f"✗ FAILED")
        print(f"Error: {e}")
        return False


async def main():
    """Run all test queries."""
    print("="*60)
    print("Wetlands MCP Server - Test Suite")
    print("Using external endpoint: https://s3-west.nautilus.io")
    print("="*60)
    
    # Base setup with external endpoint
    setup = """
SET THREADS=100; SET preserve_insertion_order=false; SET enable_object_cache=true; SET temp_directory='/tmp';
INSTALL httpfs; LOAD httpfs; INSTALL h3 FROM community; LOAD h3;
CREATE OR REPLACE SECRET s3 (TYPE S3, ENDPOINT 's3-west.nrp-nautilus.io', URL_STYLE 'path', USE_SSL true, KEY_ID '', SECRET '');
"""
    
    tests = [
        {
            "description": "Test 1: Read wetlands category codes (small CSV)",
            "query": setup + """
SELECT * FROM read_csv('s3://public-wetlands/glwd/category_codes.csv')
LIMIT 10;
"""
        },
        {
            "description": "Test 2: Count small country (Liechtenstein) hexes",
            "query": setup + """
SELECT country, name, APPROX_COUNT_DISTINCT(h8) as hex_count
FROM read_parquet('s3://public-overturemaps/hex/countries.parquet')
WHERE country = 'LI'
GROUP BY country, name;
"""
        },
        {
            "description": "Test 3: Sample wetlands data (just a few rows)",
            "query": setup + """
SELECT Z, h8, h0
FROM read_parquet('s3://public-wetlands/glwd/hex/**')
LIMIT 10;
"""
        },
        {
            "description": "Test 4: Sample Ramsar sites for one country",
            "query": setup + """
SELECT "Site name", Country, "Area (ha)"
FROM read_parquet('s3://public-wetlands/ramsar/hex/**')
WHERE Country = 'Switzerland'
GROUP BY "Site name", Country, "Area (ha)"
LIMIT 5;
"""
        },
        {
            "description": "Test 5: Simple aggregation with categories",
            "query": setup + """
SELECT c.category, COUNT(*) as count
FROM read_csv('s3://public-wetlands/glwd/category_codes.csv') c
WHERE c.Z > 0
GROUP BY c.category
LIMIT 5;
"""
        }
    ]
    
    # Server parameters
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "mcp-data-server", "--db-path", ":memory:"],
        env=None
    )
    
    results = []
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the session
            await session.initialize()
            
            # Run tests
            for test in tests:
                success = await test_query(session, test["query"], test["description"])
                results.append(success)
    
    # Summary
    print(f"\n{'='*60}")
    print("Test Summary")
    print(f"{'='*60}")
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    print(f"Failed: {total-passed}/{total}")
    
    if passed == total:
        print("\n✓ All tests passed!")
        return 0
    else:
        print(f"\n✗ {total-passed} test(s) failed")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)

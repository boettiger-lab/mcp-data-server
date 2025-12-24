#!/usr/bin/env python3
"""
Test script for the Wetlands MCP Server deployed on Kubernetes.
Tests the k8s endpoint using streamable_http transport with the MCP SDK.
"""

import asyncio
import httpx
from mcp import ClientSession
from mcp.client.session import ClientSession as BaseClientSession
from contextlib import asynccontextmanager


class StreamableHTTPClient:
    """Client for connecting to MCP server via streamable_http."""
    
    def __init__(self, endpoint_url: str):
        self.endpoint_url = endpoint_url.rstrip('/')
        self.http_client = httpx.AsyncClient(timeout=600.0)
        self._read_stream = None
        self._write_stream = None
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.http_client.aclose()
        
    @asynccontextmanager
    async def connect(self):
        """Create a connection context for the MCP session."""
        try:
            # For streamable_http, we use HTTP requests to communicate
            yield (self._read_from_server, self._write_to_server)
        finally:
            pass
    
    async def _read_from_server(self):
        """Read from the server using HTTP streaming."""
        if self._read_stream is None:
            raise RuntimeError("Read stream not initialized")
        
        try:
            async for line in self._read_stream.aiter_lines():
                if line:
                    yield line + '\n'
        except Exception as e:
            print(f"Error reading from server: {e}")
            raise
    
    async def _write_to_server(self, data: str):
        """Write to the server using HTTP POST."""
        try:
            response = await self.http_client.post(
                f"{self.endpoint_url}/mcp",
                content=data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }
            )
            
            # Store the response stream for reading
            self._read_stream = response
            
            return response
        except Exception as e:
            print(f"Error writing to server: {e}")
            raise


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
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all test queries against the k8s endpoint."""
    # The k8s endpoint URL (adjust as needed)
    endpoint_url = "https://wetlands-mcp.nrp-nautilus.io"
    
    print("="*60)
    print("Wetlands MCP Server - K8s Streamable HTTP Test Suite")
    print(f"Testing endpoint: {endpoint_url}")
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
            "description": "Test 4: Query protected areas count",
            "query": setup + """
SELECT COUNT(*) as protected_area_count
FROM read_parquet('s3://public-wetlands/wdpa/hex/**')
LIMIT 1;
"""
        },
        {
            "description": "Test 5: Sample carbon storage data",
            "query": setup + """
SELECT h8, carbon
FROM read_parquet('s3://public-wetlands/carbon/hex/**')
LIMIT 10;
"""
        }
    ]
    
    # Use streamable_http client to connect to k8s endpoint
    async with httpx.AsyncClient(timeout=600.0) as http_client:
        passed = 0
        failed = 0
        
        for test in tests:
            try:
                # Make request to the k8s endpoint
                response = await http_client.post(
                    f"{endpoint_url}/mcp/v1/tools/call",
                    json={
                        "name": "query",
                        "arguments": {
                            "query": test["query"]
                        }
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    }
                )
                
                print(f"\n{'='*60}")
                print(f"Test: {test['description']}")
                print(f"{'='*60}")
                print(f"Status Code: {response.status_code}")
                
                if response.status_code == 200:
                    result = response.json()
                    
                    # Check if we got valid content
                    if "content" in result and len(result["content"]) > 0:
                        content = result["content"][0]
                        text = content.get("text", str(content))
                        
                        if "Error executing" in text or "❌" in text:
                            print(f"✗ FAILED")
                            lines = text.split("\n")
                            preview_lines = lines[:5]
                            print("Error output:")
                            for line in preview_lines:
                                print(f"  {line}")
                            failed += 1
                        else:
                            print(f"✓ SUCCESS")
                            lines = text.split("\n")
                            preview_lines = lines[:5]
                            print("Result preview:")
                            for line in preview_lines:
                                print(f"  {line}")
                            if len(lines) > 5:
                                remaining = len(lines) - 5
                                print(f"  ... ({remaining} more lines)")
                            passed += 1
                    else:
                        print(f"✗ FAILED - No content in response")
                        failed += 1
                else:
                    print(f"✗ FAILED - HTTP {response.status_code}")
                    print(f"Response: {response.text[:200]}")
                    failed += 1
                    
            except Exception as e:
                print(f"✗ FAILED")
                print(f"Error: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
        
        # Summary
        print(f"\n{'='*60}")
        print(f"Test Summary")
        print(f"{'='*60}")
        print(f"Total tests: {len(tests)}")
        print(f"Passed: {passed}")
        print(f"Failed: {failed}")
        print(f"Success rate: {passed/len(tests)*100:.1f}%")
        print(f"{'='*60}")
        
        return passed == len(tests)


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)

import os
import sys
import httpx
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("aethviondb-mcp")

# The default Layer 1 API endpoint
AETHVIONDB_URL = os.getenv("AETHVIONDB_URL", "http://127.0.0.1:7475/api/v1/raw")
AETHVIONDB_KEY = os.getenv("AETHVIONDB_API_KEY", "")

def get_headers():
    headers = {"Content-Type": "application/json"}
    if AETHVIONDB_KEY:
        headers["X-API-Key"] = AETHVIONDB_KEY
    return headers

@mcp.tool()
async def search_entities(db: str, query: str = "", entity_type: str = "") -> dict:
    """Search for entities in the AethvionDB Layer 1.
    
    Args:
        db: The name of the database (e.g., 'default').
        query: The search string to look for.
        entity_type: Optional filter by type (e.g. 'person', 'module').
    """
    async with httpx.AsyncClient() as client:
        params = {"q": query}
        if entity_type:
            # Note: actual API might require JSON filter or different query param.
            # Assuming a simple search endpoint for now based on standard REST.
            params["filters"] = f'{{"type": "{entity_type}"}}'
            
        url = f"{AETHVIONDB_URL}/{db}/search"
        response = await client.get(url, params=params, headers=get_headers())
        response.raise_for_status()
        return response.json()

@mcp.tool()
async def get_entity(db: str, entity_id_or_name: str) -> dict:
    """Get the full details of a specific entity by ID or exact name.
    
    Args:
        db: The name of the database.
        entity_id_or_name: The ID (ws_...) or canonical name of the entity.
    """
    async with httpx.AsyncClient() as client:
        # Assuming there's a lookup or standard get endpoint
        url = f"{AETHVIONDB_URL}/{db}/entities/{entity_id_or_name}"
        response = await client.get(url, headers=get_headers())
        response.raise_for_status()
        return response.json()

@mcp.tool()
async def upsert_entity(db: str, name: str, entity_type: str = "other", summary: str = "") -> dict:
    """Create or update a basic entity in the database.
    
    Args:
        db: The name of the database.
        name: Canonical name of the entity.
        entity_type: Type of the entity (e.g., 'person', 'module', 'concept').
        summary: A short description.
    """
    async with httpx.AsyncClient() as client:
        url = f"{AETHVIONDB_URL}/{db}/entities"
        payload = {
            "name": name,
            "type": entity_type,
            "sections": {
                "core": {"summary": summary}
            }
        }
        response = await client.post(url, json=payload, headers=get_headers())
        response.raise_for_status()
        return response.json()

def main():
    mcp.run()

if __name__ == "__main__":
    main()

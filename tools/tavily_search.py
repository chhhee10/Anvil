"""
Tavily web search tool with multi-hop support.
"""
from __future__ import annotations
import os
import logging
import omium
from typing import List, Optional
from tavily import TavilyClient
from models.schemas import SearchResult

logger = logging.getLogger("newsroom.tools.tavily")

_client: Optional[TavilyClient] = None


def get_client() -> TavilyClient:
    global _client
    if _client is None:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY not set")
        _client = TavilyClient(api_key=api_key)
    return _client


@omium.trace("tavily_search")
def search(query: str, max_results: int = 5, search_depth: str = "basic") -> List[SearchResult]:
    """
    Perform a single Tavily search and return structured results.
    search_depth: 'basic' (faster, 1 credit) or 'advanced' (2 credits)
    """
    client = get_client()
    logger.info("Tavily search: '%s' (depth=%s)", query, search_depth)
    try:
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_answer=True,
        )
        results = []
        for r in response.get("results", []):
            results.append(SearchResult(
                query=query,
                url=r.get("url", ""),
                title=r.get("title", ""),
                content=r.get("content", "")[:1500],  # cap content length
                score=r.get("score", 0.0),
            ))
        logger.info("Tavily returned %d results for '%s'", len(results), query)
        return results
    except Exception as e:
        logger.error("Tavily search failed for '%s': %s", query, e)
        return []


@omium.trace("multi_hop_search")
def multi_hop_search(
    initial_query: str,
    follow_up_fn=None,
    max_hops: int = 3,
) -> List[SearchResult]:
    """
    Multi-hop search: first search informs the next query.
    follow_up_fn: callable(results) -> str (generates follow-up query from results)
    Falls back to 3 independent searches if no follow_up_fn provided.
    """
    all_results: List[SearchResult] = []
    query = initial_query

    for hop in range(max_hops):
        results = search(query, max_results=4)
        all_results.extend(results)
        if not results or hop == max_hops - 1:
            break
        if follow_up_fn:
            # Generate next query based on what we found
            next_query = follow_up_fn(results, hop)
            if not next_query or next_query == query:
                break
            query = next_query
            logger.info("Multi-hop %d: next query = '%s'", hop + 1, query)

    # Deduplicate by URL
    seen = set()
    unique = []
    for r in all_results:
        if r.url not in seen:
            seen.add(r.url)
            unique.append(r)
    return unique

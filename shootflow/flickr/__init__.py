"""
Flickr integration package.

Exports:
- FlickrAPIClient: the Flickr API wrapper
- suggest_groups_via_llm: LLM-based group suggester
"""

from .flickr import FlickrAPIClient
from .group_suggester_llm import suggest_groups_via_llm

__all__ = ["FlickrAPIClient", "suggest_groups_via_llm"]

"""Anthropic prompt caching helper — adds cache_control to messages so repeated
system prompts / long contexts cost 10% of full price.
Usage: import this in any bridge that calls Anthropic API directly.
"""
def add_cache_control(messages, threshold=2048):
    """Add cache_control to the longest system message if it's over threshold chars.
    Anthropic cache: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
    Requires anthropic-beta: prompt-caching-2024-07-31 header."""
    if not messages: return messages
    for m in messages:
        if m.get('role') == 'system' and isinstance(m.get('content'), str):
            if len(m['content']) >= threshold:
                # Convert to structured content with cache marker
                m['content'] = [{'type': 'text', 'text': m['content'],
                                  'cache_control': {'type': 'ephemeral'}}]
                break
    return messages

"""Extraction layer.

Two extractors, deliberately:

*   `rules` is a deterministic recall net. It is cheap, it is auditable, and it runs on
    every message. It over-fires and it is supposed to.
*   `claude_extractor` is the judgment layer. It classifies, resolves ambiguity, and
    scores confidence.

The reason both exist is that a missed promise is a false negative nobody ever sees. A
model-only pipeline that misses a commitment produces a clean-looking report and a broken
promise. Rules catch the phrasing; the model decides what it means.
"""

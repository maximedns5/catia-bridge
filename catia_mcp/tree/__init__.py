"""Replay a CAD feature tree (version 1) in CATIA as native, editable features.

The tree is the CAD-neutral JSON a build123d model is traced into: sketches
with lines, arcs and circles on planes given in world coordinates, then
extrudes, combines, holes and the rest, in construction order. This package
checks a tree (preflight), replays it through CATIA Automation (builder), and
can compute the expected solids with build123d (reference).

Nothing here exposes CATIA to an agent or runs code from the tree: the tree is
data, and only the feature types listed in preflight.SUPPORTED are built.
"""

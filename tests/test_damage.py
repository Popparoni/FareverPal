"""Cluster-scan range derivation (headless, pure).

These cover the logic that turns located DamageDisplay instance addresses into
the small set of memory ranges `poll()` scans each tick — the fix for the live-
enumeration wall (see DPS_METER_PLAN). The live scan itself needs the game; here
we lock down the pure address math.
"""
from farever_companion.core.damage import (
    cluster_ranges, clip_ranges, RANGE_PAD, RANGE_MERGE_GAP,
)


def _covers(ranges, addr):
    return any(base <= addr < base + length for base, length in ranges)


def test_single_address_one_padded_range():
    a = 0x1_0000_0000
    ranges = cluster_ranges([a])
    assert len(ranges) == 1
    base, length = ranges[0]
    assert base == a - RANGE_PAD
    assert length == 2 * RANGE_PAD
    assert _covers(ranges, a)


def test_nearby_addresses_merge():
    a = 0x1_0000_0000
    b = a + RANGE_MERGE_GAP        # within the merge gap -> one range
    ranges = cluster_ranges([a, b])
    assert len(ranges) == 1
    assert _covers(ranges, a) and _covers(ranges, b)


def test_far_addresses_split():
    a = 0x1_0000_0000
    b = a + 100 * RANGE_MERGE_GAP  # far apart -> two ranges
    ranges = cluster_ranges([a, b])
    assert len(ranges) == 2
    assert _covers(ranges, a) and _covers(ranges, b)


def test_dedup_and_sort_unordered_input():
    a, b = 0x2_0000_0000, 0x1_0000_0000
    ranges = cluster_ranges([a, b, a, b])     # duplicates + reversed
    # sorted ascending, far apart -> two ranges, low one first
    assert ranges[0][0] < ranges[1][0]
    assert _covers(ranges, a) and _covers(ranges, b)


def test_empty_and_garbage_addresses():
    assert cluster_ranges([]) == []
    assert cluster_ranges([0, 0]) == []       # 0 is not a real instance


def test_no_negative_base():
    ranges = cluster_ranges([0x10])           # pad would underflow below 0
    assert ranges[0][0] == 0


def test_clip_to_regions_intersects():
    # one padded range; a committed region covers only its right half
    a = 0x1_0000_0000
    ranges = cluster_ranges([a])
    base, length = ranges[0]
    mid = base + length // 2
    regions = [(mid, length, 0x04, True)]     # (base, size, protect, writable)
    clipped = clip_ranges(ranges, regions)
    assert clipped == [(mid, base + length - mid)]


def test_clip_drops_ranges_outside_regions():
    ranges = cluster_ranges([0x1_0000_0000])
    # a region nowhere near the range -> nothing survives
    assert clip_ranges(ranges, [(0x9_0000_0000, 0x1000, 0x04, True)]) == []


def test_clip_spanning_multiple_regions():
    a = 0x1_0000_0000
    ranges = cluster_ranges([a])
    base, length = ranges[0]
    # two adjacent committed regions that together cover the whole range
    half = length // 2
    regions = [(base, half, 0x04, True), (base + half, length - half, 0x04, True)]
    clipped = clip_ranges(ranges, regions)
    assert sum(l for _, l in clipped) == length
    assert _covers(clipped, a)


def test_clip_empty_inputs():
    assert clip_ranges([], [(0, 0x1000, 0x04, True)]) == []
    assert clip_ranges([(0x1000, 0x100)], []) == []

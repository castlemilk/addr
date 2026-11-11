"""Conservative observation checks before fitting a single roof hypothesis."""
from __future__ import annotations


def narrow_connections(polygon):
    """Find substantial roof lobes joined only through a sub-1.5 m neck.

    This does not prove separate buildings: real breezeways exist. Keep the
    original footprint and ask for connection evidence instead of inventing
    one continuous pitched roof. Coordinates must be in local metres.
    """
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        return None
    inset = polygon.buffer(-.75, join_style='mitre')
    pieces = [inset] if inset.geom_type == 'Polygon' else list(getattr(inset, 'geoms', []))
    areas = sorted((part.area for part in pieces if part.geom_type == 'Polygon'
                    and part.area >= max(6., polygon.area*.06)), reverse=True)
    if len(areas) < 2:
        return None
    return {'neck_width_threshold_m': 1.5, 'substantial_lobes': len(areas),
            'inset_lobe_areas_m2': [round(a, 3) for a in areas],
            'interpretation': 'Possible merged roof sections or a real narrow connector; verify in imagery.'}

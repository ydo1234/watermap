"""Reproducible before/after benchmark using the same fully checked route vertices."""
import math
import os
import secrets
import tempfile
import time


def haversine(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta = math.radians(lon2-lon1)
    a = math.sin((phi2-phi1)/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta/2)**2
    return 6_371_000*2*math.atan2(math.sqrt(a),math.sqrt(max(0,1-a)))


def run():
    os.environ['SECRET_KEY'] = secrets.token_urlsafe(48)
    with tempfile.TemporaryDirectory() as directory:
        os.environ['WATERMAP_DATA_DIR'] = directory
        import app
        from spatial import RouteIndex
        # Dense 300-point route across the Paris region, no subsampling in either run.
        track = [(48.65+i*0.001, 2.15+0.08*math.sin(i/45)) for i in range(300)]
        references = app.parse_water_points()+app.parse_toilets()
        lats, lons = zip(*track)
        candidates = [p for p in references if min(lats)-0.01 <= p['lat'] <= max(lats)+0.01 and min(lons)-0.01 <= p['lon'] <= max(lons)+0.01]
        started = time.perf_counter()
        old = [p for p in candidates if min(haversine(p['lat'],p['lon'],lat,lon) for lat,lon in track) <= 500]
        old_seconds = time.perf_counter()-started
        started = time.perf_counter()
        index = RouteIndex(track)
        new = app.nearby_points(app.parse_water_points(),index,500) + app.nearby_points(app.parse_toilets(),index,500)
        new_seconds = time.perf_counter()-started
        print({'reference_points':len(references),'route_vertices':len(track),'old_bbox_candidates':len(candidates),'old_seconds':round(old_seconds,3),'new_seconds':round(new_seconds,3),'speedup':round(old_seconds/new_seconds,2),'old_matches':len(old),'new_matches':len(new),'segment_checks':index.checks})


if __name__ == '__main__':
    run()

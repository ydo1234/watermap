import math
import random

import pytest

import gpx_utils
from gpx_utils import parse_track, TrackPoints
from spatial import RouteIndex, vector, arc_distance


def parse(tmp_path, text):
    path = tmp_path/'track.gpx'
    path.write_text(text)
    return parse_track(path)


@pytest.mark.parametrize('coordinate', ['nan', 'inf', '-91', '91'])
def test_invalid_coordinates(tmp_path, coordinate):
    with pytest.raises(ValueError, match='Coordonnées'):
        parse(tmp_path, f'<gpx><trk><trkseg><trkpt lat="{coordinate}" lon="2"/></trkseg></trk></gpx>')


@pytest.mark.parametrize('payload', [
    '<!DOCTYPE gpx [<!ENTITY x "boom">]><gpx>&x;</gpx>',
    '<!DOCTYPE gpx SYSTEM "file:///etc/passwd"><gpx/>',
    '<html><body>Not a GPX</body></html>', '<gpx><trk>', '<gpx/>',
])
def test_untrusted_xml_rejected(tmp_path, payload):
    with pytest.raises(ValueError):
        parse(tmp_path, payload)


def test_points_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(gpx_utils, 'MAX_TRACK_POINTS', 1)
    with pytest.raises(ValueError, match='100 000'):
        parse(tmp_path, '<gpx><rte><rtept lat="48" lon="2"/><rtept lat="49" lon="3"/></rte></gpx>')


def test_file_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(gpx_utils, 'MAX_FILE_BYTES', 4)
    with pytest.raises(ValueError, match='20 Mo'):
        parse(tmp_path, '<gpx/>')


def test_namespaced_segments_and_routes(tmp_path):
    points = parse(tmp_path, '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg><trkpt lat="48" lon="2"/></trkseg><trkseg><trkpt lat="49" lon="3"/></trkseg></trk><rte><rtept lat="50" lon="4"/></rte></gpx>')
    assert points == [(48,2), (49,3), (50,4)]
    assert points.segment_starts == {0,1,2}


def test_mid_segment_water_not_missed():
    route = RouteIndex([(0, 0), (0, 2)])
    assert route.distance(0.001, 1, 500) == pytest.approx(111.195, abs=0.01)
    assert math.isinf(route.distance(0.1, 1, 500))


def test_disconnected_segments_do_not_create_phantom_route():
    points = TrackPoints()
    points.extend([(0,0), (0,2)])
    points.segment_starts = {0,1}
    assert math.isinf(RouteIndex(points).distance(0,1,500))


def test_date_line_and_poles():
    assert RouteIndex([(0,179), (0,-179)]).distance(0,180,500) == pytest.approx(0, abs=1e-6)
    assert math.isinf(RouteIndex([(0,179), (0,-179)]).distance(0,0,500))
    assert RouteIndex([(89,0), (89,180)]).distance(90,0,500) == pytest.approx(0, abs=1e-6)


def test_bvh_matches_full_segment_search():
    rng = random.Random(42)
    for offset in (0, 48, 80):
        track = [(offset + rng.uniform(-0.2,0.2), 2 + rng.uniform(-0.2,0.2)) for _ in range(100)]
        route = RouteIndex(track)
        for _ in range(50):
            lat, lon = offset + rng.uniform(-0.3,0.3), 2+rng.uniform(-0.3,0.3)
            expected = min(arc_distance(vector(lat,lon), vector(*a), vector(*b)) for a,b in zip(track,track[1:]))
            result = route.distance(lat,lon,5000)
            if expected <= 5000:
                assert result == pytest.approx(expected, abs=1e-5)
            else:
                assert math.isinf(result)

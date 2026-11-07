import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from conftest import account
from gpx_export import enriched_gpx, map_points, GPX_11

WATER = [{'lat': 48.8501, 'lon': 2.3522, 'name': 'Fontaine & parc <centre>'}]
TOILETS = [{'lat': 48.8502, 'lon': 2.3523, 'name': 'Toilettes publiques'}]
ORIGINAL = b'''<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" xmlns:x="urn:test" version="1.1" creator="GPS"><metadata><name>Tour</name></metadata><wpt lat="48" lon="2"><name>Personnel</name></wpt><trk><name>Tour</name><trkseg><trkpt lat="48.85" lon="2.35"><ele>123.4</ele><time>2026-11-07T08:00:00Z</time><extensions><x:hr>120</x:hr></extensions></trkpt></trkseg><trkseg><trkpt lat="48.86" lon="2.36"/></trkseg></trk><extensions><x:note>Original</x:note></extensions></gpx>'''


def page_data(water=WATER, toilets=TOILETS):
    return '<nav><a href="/">Retour</a></nav><script>\n    const waterPoints = ' + json.dumps(water) + ';\n    const toiletPoints = ' + json.dumps(toilets) + ';\n</script>'


def saved_route(module, user, name='tour', original=ORIGINAL, content=None):
    upload = Path(module.get_user_upload_folder(user.id)) / (name + '.gpx')
    page = Path(module.get_user_page_folder(user.id)) / (name + '.html')
    upload.write_bytes(original)
    page.write_text(page_data() if content is None else content, encoding='utf-8')
    return upload, page


def test_export_preserves_track_metadata_and_original_waypoints():
    original = ET.fromstring(ORIGINAL)
    result = ET.fromstring(enriched_gpx(ORIGINAL, WATER, TOILETS))
    q = lambda name: '{' + GPX_11 + '}' + name
    for name in ('metadata', 'trk', 'extensions'):
        assert ET.tostring(result.find(q(name))) == ET.tostring(original.find(q(name)))
    assert [c.tag.rsplit('}', 1)[-1] for c in result] == ['metadata', 'wpt', 'wpt', 'wpt', 'trk', 'extensions']
    points = result.findall(q('wpt'))
    assert points[0].findtext(q('name')) == 'Personnel'
    assert points[1].findtext(q('name')) == 'EAU — Fontaine & parc <centre>'
    assert points[1].findtext(q('sym')) == 'Drinking Water'
    assert points[2].findtext(q('sym')) == 'Restroom'
    assert points[2].findtext(q('type')) == 'Toilets'
    assert float(points[1].attrib['lat']) == WATER[0]['lat']


def test_repeat_export_replaces_owned_points_and_keeps_both_types_at_same_place():
    water = WATER * 2
    toilet = [{**WATER[0], 'name': 'WC'}]
    once = enriched_gpx(ORIGINAL, water, toilet)
    twice = enriched_gpx(once, water, toilet)
    assert twice == once
    assert len(ET.fromstring(twice).findall('{' + GPX_11 + '}wpt')) == 3


@pytest.mark.parametrize('namespace', ['', 'http://www.topografix.com/GPX/1/0', GPX_11])
def test_namespace_and_empty_selection(namespace):
    xml = ('<gpx xmlns="' + namespace + '"><rte><rtept lat="48" lon="2"/></rte></gpx>').encode()
    result = ET.fromstring(enriched_gpx(xml, [], []))
    assert result.tag == ET.fromstring(xml).tag
    assert len(result) == 1


@pytest.mark.parametrize('xml', [b'<html/>', b'<gpx>', b'<!DOCTYPE gpx [<!ENTITY x "boom">]><gpx>&x;</gpx>'])
def test_rejects_invalid_or_unsafe_gpx(xml):
    with pytest.raises(ValueError):
        enriched_gpx(xml, WATER, TOILETS)


def test_json_is_data_and_cannot_spoof_second_declaration():
    water = [{**WATER[0], 'name': 'const toiletPoints = [];\n<script>alert(1)</script>'}]
    assert map_points(page_data(water, TOILETS)) == [water, TOILETS]
    with pytest.raises(ValueError):
        map_points('const waterPoints = alert(1);\nconst toiletPoints = [];')
    with pytest.raises(ValueError):
        map_points(page_data([{'lat': float('nan'), 'lon': 2, 'name': 'x'}], []))


def test_download_and_buttons_leave_saved_files_untouched(module, client, owner):
    upload, page = saved_route(module, owner)
    before = upload.read_bytes(), page.read_bytes()
    response = client.get('/itineraries/tour/enriched.gpx')
    assert response.status_code == 200
    assert response.mimetype == 'application/gpx+xml'
    assert 'tour-watermap.gpx' in response.headers['Content-Disposition']
    assert len(ET.fromstring(response.data).findall('{' + GPX_11 + '}wpt')) == 3
    assert b'/itineraries/tour/enriched.gpx' in client.get('/').data
    detail = client.get('/generated/tour')
    assert detail.status_code == 200
    assert b'/itineraries/tour/enriched.gpx' in detail.data
    assert (upload.read_bytes(), page.read_bytes()) == before


def test_other_users_routes_are_not_accessible(module, client, owner):
    other = account(module, 'other@example.test')
    saved_route(module, other, name='private')
    assert client.get('/itineraries/private/enriched.gpx').status_code == 404
    saved_route(module, owner, name='private')
    assert client.get('/itineraries/private/enriched.gpx').status_code == 200


def test_login_required(module, client):
    assert client.get('/itineraries/tour/enriched.gpx').status_code == 302


def test_missing_invalid_and_in_progress_routes(module, client, owner):
    assert client.get('/itineraries/missing/enriched.gpx').status_code == 404
    saved_route(module, owner, original=b'<gpx>')
    assert client.get('/itineraries/tour/enriched.gpx').status_code == 422
    module.active_names.add((owner.id, 'tour'))
    try:
        assert client.get('/itineraries/tour/enriched.gpx').status_code == 409
    finally:
        module.active_names.discard((owner.id, 'tour'))
    assert client.get('/itineraries/..%5Ctour/enriched.gpx').status_code == 404


def test_legacy_map_without_json_reports_how_to_recover(module, client, owner):
    saved_route(module, owner, content='<nav></nav>')
    response = client.get('/itineraries/tour/enriched.gpx')
    assert response.status_code == 422
    assert 'Recréez' in response.text

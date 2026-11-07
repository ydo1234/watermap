from datetime import datetime
import math
import random
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from tcx_export import enriched_tcx, course_vertices, NearestVertex, TCX
from spatial import vector
from test_gpx_export import ORIGINAL, WATER, TOILETS, saved_route

Q = lambda name: '{' + TCX + '}' + name


def test_tcx_explicit_types_coordinates_and_anchored_times():
    root=ET.fromstring(enriched_tcx(ORIGINAL,WATER,TOILETS,'Parcours de démonstration très long'))
    course=root.find('.//'+Q('Course'))
    assert len(course.findtext(Q('Name')))<=15
    tracks=course.findall(Q('Track'))
    assert len(tracks)==2
    trackpoints=course.findall('.//'+Q('Trackpoint'))
    assert len(trackpoints)==2
    assert trackpoints[0].findtext(Q('AltitudeMeters'))=='123.4'
    assert trackpoints[1].find(Q('AltitudeMeters')) is None
    times=[p.findtext(Q('Time')) for p in trackpoints]
    assert times==sorted(times) and len(set(times))==2
    # Disconnected segments must not add an invented straight-line distance.
    assert [p.findtext(Q('DistanceMeters')) for p in trackpoints]==['0.000','0.000']
    points=course.findall(Q('CoursePoint'))
    assert len(points)==2
    assert {p.findtext(Q('PointType')) for p in points}=={'Water','Generic'}
    water=next(p for p in points if p.findtext(Q('PointType'))=='Water')
    assert float(water.find('.//'+Q('LatitudeDegrees')).text)==WATER[0]['lat']
    assert float(water.find('.//'+Q('LongitudeDegrees')).text)==WATER[0]['lon']
    assert 'Fontaine & parc <centre>' in water.findtext(Q('Notes'))
    assert all(p.findtext(Q('Time')) in times for p in points)
    assert all(len(p.findtext(Q('Name')))<=10 for p in points)


def test_distance_time_and_coursepoint_order():
    xml=b'<gpx><trk><trkseg><trkpt lat="0" lon="0"/><trkpt lat="0" lon="0.01"/></trkseg></trk></gpx>'
    points=[{'lat':0,'lon':0.009,'name':'Fin'},{'lat':0,'lon':0.001,'name':'Debut'}]
    root=ET.fromstring(enriched_tcx(xml,points,[],'test'))
    track=root.findall('.//'+Q('Trackpoint'))
    assert float(track[1].findtext(Q('DistanceMeters')))==pytest.approx(1111.95,abs=0.1)
    assert [p.findtext(Q('Notes')) for p in root.findall('.//'+Q('CoursePoint'))]==['EAU — Debut (Watermap)','EAU — Fin (Watermap)']
    assert len(root.findall('.//'+Q('CoursePoint')))==2


def test_duplicate_pois_coalesce_but_keep_distinct_types():
    root=ET.fromstring(enriched_tcx(ORIGINAL,WATER*2,WATER,'test'))
    assert len(root.findall('.//'+Q('CoursePoint')))==2


def test_nearest_vertex_matches_brute_force():
    rng=random.Random(91)
    points=[(rng.uniform(-80,80),rng.uniform(-180,180),None) for _ in range(200)]
    index=NearestVertex(points)
    for _ in range(50):
        lat,lon=rng.uniform(-90,90),rng.uniform(-180,180)
        query=vector(lat,lon)
        expected=min(range(len(points)),key=lambda i:sum((a-b)**2 for a,b in zip(query,vector(*points[i][:2]))))
        assert index.nearest(lat,lon)==expected


@pytest.mark.parametrize('xml',[b'<html/>',b'<gpx/>',b'<!DOCTYPE gpx [<!ENTITY x "boom">]><gpx>&x;</gpx>'])
def test_invalid_xml_rejected(xml):
    with pytest.raises(ValueError):
        enriched_tcx(xml,WATER,TOILETS,'test')


def test_download_tcx_is_private_and_leaves_gpx_unchanged(module,client,owner):
    upload,page=saved_route(module,owner)
    before=upload.read_bytes(),page.read_bytes()
    response=client.get('/itineraries/tour/garmin.tcx')
    assert response.status_code==200
    assert response.mimetype=='application/vnd.garmin.tcx+xml'
    assert 'tour-watermap.tcx' in response.headers['Content-Disposition']
    root=ET.fromstring(response.data)
    assert len(root.findall('.//'+Q('CoursePoint')))==2
    assert client.get('/itineraries/absent/garmin.tcx').status_code==404
    assert client.get('/itineraries/tour/enriched.gpx').mimetype=='application/gpx+xml'
    for route in ('/','/generated/tour'):
        html=client.get(route).text
        assert '/itineraries/tour/garmin.tcx' in html
        assert 'data-gpx-name="tour-watermap.tcx"' in html
        assert '/itineraries/tour/enriched.gpx' in html
    assert (upload.read_bytes(),page.read_bytes())==before


def test_tcx_requires_login(module,client):
    assert client.get('/itineraries/tour/garmin.tcx').status_code==302

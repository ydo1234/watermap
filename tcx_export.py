"""A Garmin TCX Course with explicit, ordered water/toilet CoursePoints."""
from datetime import datetime, timedelta, timezone
import math
import unicodedata
import xml.etree.ElementTree as ET  # nosec B405: serialization only
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from gpx_utils import coordinate, MAX_FILE_BYTES, MAX_TRACK_POINTS
from gpx_export import MAX_POIS, GPX_10, GPX_11
from spatial import vector, angle, EARTH_RADIUS

TCX = 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'
# A named prefix avoids changing ElementTree's process-wide default GPX namespace.
ET.register_namespace('tcx', TCX)


def tag(name):
    return '{' + TCX + '}' + name


def local(element):
    return element.tag.rsplit('}', 1)[-1]


class NearestVertex:
    def __init__(self, vertices):
        self.checks = 0
        def build(entries):
            if not entries:
                return None
            axis = max(range(3), key=lambda a: max(p[0][a] for p in entries)-min(p[0][a] for p in entries))
            entries.sort(key=lambda p: (p[0][axis], p[1]))
            middle = len(entries)//2
            point, index = entries[middle]
            return (point, index, axis, build(entries[:middle]), build(entries[middle+1:]))
        self.root = build([(vector(p[0], p[1]), i) for i, p in enumerate(vertices)])

    def nearest(self, lat, lon):
        query = vector(lat, lon)
        best = (math.inf, math.inf)
        def visit(node):
            nonlocal best
            if node is None:
                return
            self.checks += 1
            if self.checks > 5_000_000:
                raise ValueError('Trop de points pour cet export Garmin. Réduisez la distance de recherche.')
            point, index, axis, left, right = node
            best = min(best, (sum((a-b)**2 for a,b in zip(query, point)), index))
            delta = query[axis]-point[axis]
            near, far = (left, right) if delta < 0 else (right, left)
            visit(near)
            if delta*delta <= best[0]:
                visit(far)
        visit(self.root)
        return best[1]


def course_vertices(original):
    if len(original) > MAX_FILE_BYTES:
        raise ValueError('Le fichier dépasse la limite de 20 Mo.')
    try:
        root = SafeET.fromstring(original, forbid_dtd=True)
    except (SafeET.ParseError, DefusedXmlException) as error:
        raise ValueError('Fichier GPX invalide.') from error
    ns = root.tag.split('}')[0][1:] if root.tag.startswith('{') else ''
    if local(root) != 'gpx' or ns not in ('', GPX_10, GPX_11):
        raise ValueError('Le fichier doit être un GPX.')
    vertices, breaks = [], set()
    try:
        for segment in root.iter():
            if local(segment) not in ('trkseg', 'rte'):
                continue
            breaks.add(len(vertices))
            for point in segment:
                if local(point) not in ('trkpt', 'rtept'):
                    continue
                lat, lon = coordinate(point.attrib['lat'], point.attrib['lon'])
                altitude = next((float(c.text) for c in point if local(c) == 'ele' and c.text), None)
                if altitude is not None and not math.isfinite(altitude):
                    altitude = None
                vertices.append((lat, lon, altitude))
                if len(vertices) > MAX_TRACK_POINTS:
                    raise ValueError('Le tracé dépasse la limite de 100 000 points.')
    except (KeyError, TypeError) as error:
        raise ValueError('Coordonnées invalides dans le tracé GPX.') from error
    if not vertices:
        raise ValueError('Le fichier ne contient aucun point de tracé valide.')
    return vertices, breaks


def short_name(value, fallback, maximum=15):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return (' '.join(value.split()) or fallback)[:maximum]


def position(parent, lat, lon):
    item = ET.SubElement(parent, tag('Position'))
    ET.SubElement(item, tag('LatitudeDegrees')).text = f'{lat:.8f}'
    ET.SubElement(item, tag('LongitudeDegrees')).text = f'{lon:.8f}'


def enriched_tcx(original, water, toilets, name):
    if len(water)+len(toilets) > MAX_POIS:
        raise ValueError('Trop de points pour cet export Garmin.')
    vertices, breaks = course_vertices(original)
    distances, times = [], []
    distance, elapsed = 0.0, 0.0
    # TCX requires a time on every point. This is a planned 18 km/h timeline,
    # never recorded activity data; original GPX timestamps are not rewritten.
    base = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for i, point in enumerate(vertices):
        step = 0.0 if i in breaks else angle(vector(*vertices[i-1][:2]), vector(*point[:2]))*EARTH_RADIUS
        distance += step
        if distance > 50_000_000:
            raise ValueError('Le parcours est trop long pour cet export Garmin.')
        if i:
            elapsed += max(step/5.0, 0.001)
        distances.append(distance)
        times.append((base+timedelta(seconds=elapsed)).isoformat(timespec='milliseconds').replace('+00:00', 'Z'))
    root = ET.Element(tag('TrainingCenterDatabase'))
    course = ET.SubElement(ET.SubElement(root, tag('Courses')), tag('Course'))
    ET.SubElement(course, tag('Name')).text = short_name(name, 'Watermap')
    lap = ET.SubElement(course, tag('Lap'))
    ET.SubElement(lap, tag('TotalTimeSeconds')).text = f'{elapsed:.3f}'
    ET.SubElement(lap, tag('DistanceMeters')).text = f'{distance:.3f}'
    ET.SubElement(lap, tag('Intensity')).text = 'Active'
    track = None
    for i, (lat, lon, altitude) in enumerate(vertices):
        if i in breaks:
            track = ET.SubElement(course, tag('Track'))
        point = ET.SubElement(track, tag('Trackpoint'))
        ET.SubElement(point, tag('Time')).text = times[i]
        position(point, lat, lon)
        if altitude is not None:
            ET.SubElement(point, tag('AltitudeMeters')).text = str(altitude)
        ET.SubElement(point, tag('DistanceMeters')).text = f'{distances[i]:.3f}'
    ET.SubElement(course, tag('Notes')).text = 'Watermap : eau et toilettes. Horaires indicatifs de parcours à 18 km/h, sans activité enregistrée.'
    index = NearestVertex(vertices)
    candidates, seen = [], set()
    for points, kind, prefix in ((water, 'Water', 'EAU'), (toilets, 'Generic', 'WC')):
        for point in points:
            lat, lon = coordinate(point['lat'], point['lon'])
            key = (prefix, round(lat, 6), round(lon, 6))
            if key in seen:
                continue
            seen.add(key)
            anchor = index.nearest(lat, lon)
            candidates.append((anchor, prefix, kind, lat, lon, point['name']))
    for number, (anchor, prefix, kind, lat, lon, label) in enumerate(sorted(candidates), 1):
        point = ET.SubElement(course, tag('CoursePoint'))
        ET.SubElement(point, tag('Name')).text = short_name(f'{prefix}{number} {label}', prefix, maximum=10)
        ET.SubElement(point, tag('Time')).text = times[anchor]
        position(point, lat, lon)
        # Keep real POI coordinates; no invented POI elevation or snapping to road.
        ET.SubElement(point, tag('PointType')).text = kind
        ET.SubElement(point, tag('Notes')).text = f'{prefix} — {label} (Watermap)'
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)

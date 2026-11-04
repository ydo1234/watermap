"""Bounded, entity-free parsing of uploaded GPX tracks."""
import math
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_TRACK_POINTS = 100_000
MAX_FILE_BYTES = 20 * 1024 * 1024


class TrackPoints(list):
    def __init__(self):
        super().__init__()
        self.segment_starts = set()


def coordinate(lat, lon):
    lat, lon = float(lat), float(lon)
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Coordonnées hors limites dans le tracé.')
    return lat, lon


def parse_track(path):
    points = TrackPoints()
    try:
        with open(path, 'rb') as source:
            source.seek(0, 2)
            if source.tell() > MAX_FILE_BYTES:
                raise ValueError('Le fichier dépasse la limite de 20 Mo.')
            source.seek(0)
            root_seen = False
            segment_start = True
            for event, element in SafeET.iterparse(source, events=('start', 'end'), forbid_dtd=True):
                tag = element.tag.rsplit('}', 1)[-1]
                if not root_seen:
                    if tag != 'gpx':
                        raise ValueError('Le fichier doit être un tracé GPX, et non une page HTML ou un autre format.')
                    root_seen = True
                if event == 'start' and tag in ('trkseg', 'rte'):
                    segment_start = True
                if event == 'end':
                    if tag in ('trkpt', 'rtept'):
                        try:
                            point = coordinate(element.attrib['lat'], element.attrib['lon'])
                        except (ValueError, KeyError, TypeError) as error:
                            raise ValueError('Coordonnées invalides dans le tracé GPX.') from error
                        if len(points) >= MAX_TRACK_POINTS:
                            raise ValueError('Le tracé dépasse la limite de 100 000 points.')
                        if segment_start:
                            points.segment_starts.add(len(points))
                            segment_start = False
                        points.append(point)
                    element.clear()
    except (SafeET.ParseError, DefusedXmlException) as error:
        raise ValueError('Fichier GPX invalide : XML incorrect ou déclaration d’entités interdite.') from error
    if not points:
        raise ValueError('Le fichier ne contient aucun point de tracé valide.')
    return points

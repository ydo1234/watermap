"""Export a route with the POIs already selected for its saved map."""
import json
import re
import xml.etree.ElementTree as ET  # nosec B405: serialization only
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from gpx_utils import coordinate, MAX_FILE_BYTES

MAX_MAP_BYTES = 64 * 1024 * 1024
MAX_POIS = 10000
GPX_11 = 'http://www.topografix.com/GPX/1/1'
GPX_10 = 'http://www.topografix.com/GPX/1/0'
ET.register_namespace('', GPX_11)


def map_points(content):
    """Read JSON declarations, never execute the saved page's JavaScript."""
    groups = []
    for variable in ('waterPoints', 'toiletPoints'):
        match = re.search(r'^\s*const\s+' + variable + r'\s*=\s*', content, re.MULTILINE)
        if match is None:
            raise ValueError('Cette ancienne carte ne contient pas les points exportables. Recréez la page depuis son GPX.')
        try:
            points, end = json.JSONDecoder().raw_decode(content[match.end():])
            if not content[match.end()+end:].lstrip().startswith(';') or not isinstance(points, list):
                raise ValueError()
            if len(points) > MAX_POIS:
                raise ValueError()
            for point in points:
                if not isinstance(point, dict) or not isinstance(point.get('name'), str):
                    raise ValueError()
                coordinate(point['lat'], point['lon'])
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError('Les points de cette carte sont invalides ou trop nombreux. Recréez la page.') from error
        groups.append(points)
    if sum(map(len, groups)) > MAX_POIS:
        raise ValueError('Trop de points pour cet export. Réduisez la distance de recherche.')
    return groups


def enriched_gpx(original, water, toilets):
    if len(original) > MAX_FILE_BYTES:
        raise ValueError('Le fichier dépasse la limite de 20 Mo.')
    if len(water) + len(toilets) > MAX_POIS:
        raise ValueError('Trop de points pour cet export.')
    try:
        root = SafeET.fromstring(original, forbid_dtd=True)
    except (SafeET.ParseError, DefusedXmlException) as error:
        raise ValueError('Fichier GPX invalide.') from error
    namespace = root.tag.split('}')[0][1:] if root.tag.startswith('{') else ''
    if root.tag.rsplit('}', 1)[-1] != 'gpx' or namespace not in ('', GPX_11, GPX_10):
        raise ValueError('Le fichier doit être un GPX.')
    def tag(name):
        return '{' + namespace + '}' + name if namespace else name
    # Replace only our own exported POIs when an enriched GPX is imported again.
    for child in list(root):
        if child.tag == tag('wpt') and child.findtext(tag('src')) == 'Watermap' and child.findtext(tag('type')) in ('Drinking Water', 'Toilets'):
            root.remove(child)
    insert_at = next((i for i, child in enumerate(root) if child.tag in (tag('rte'), tag('trk'), tag('extensions'))), len(root))
    seen = set()
    for points, kind, prefix, symbol in ((water, 'Drinking Water', 'EAU', 'Drinking Water'), (toilets, 'Toilets', 'WC', 'Restroom')):
        for point in points:
            lat, lon = coordinate(point['lat'], point['lon'])
            key = (kind, round(lat, 6), round(lon, 6))
            if key in seen:
                continue
            seen.add(key)
            waypoint = ET.Element(tag('wpt'), lat=f'{lat:.7f}', lon=f'{lon:.7f}')
            ET.SubElement(waypoint, tag('name')).text = prefix + ' — ' + point['name']
            ET.SubElement(waypoint, tag('desc')).text = 'Point référencé dans Watermap, à proximité du parcours.'
            ET.SubElement(waypoint, tag('src')).text = 'Watermap'
            ET.SubElement(waypoint, tag('sym')).text = symbol
            ET.SubElement(waypoint, tag('type')).text = kind
            root.insert(insert_at, waypoint)
            insert_at += 1
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)

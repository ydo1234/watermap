import html
import json
import math
import os
import re
import threading
import uuid
import xml.etree.ElementTree as ET
from flask import Flask, request, redirect, url_for, render_template, send_from_directory, jsonify
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
PAGE_FOLDER = os.path.join(BASE_DIR, 'pages')
ALLOWED_EXTENSIONS = {'gpx'}
WATER_GPX = os.path.join(BASE_DIR, 'water.gpx')
WC_JSON = os.path.join(BASE_DIR, 'wc.json')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PAGE_FOLDER, exist_ok=True)

# Cache global pour les points d'eau
_water_points_cache = None
_water_points_lock = threading.Lock()

# Cache global pour les restaurants
_restaurants_cache = None
_restaurants_lock = threading.Lock()

def load_existing_pages():
    """Charger les pages existantes au démarrage."""
    pass

def load_water_points_cache():
    """Précharger les points d'eau au démarrage pour optimiser les performances."""
    global _water_points_cache
    try:
        if os.path.exists(WATER_GPX):
            print("Préchargement des points d'eau...")
            with open(WATER_GPX, 'r', encoding='utf-8', errors='replace') as f:
                raw_xml = f.read()
            fixed_xml = re.sub(r'&(?!([A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)', '&amp;', raw_xml)
            root = ET.fromstring(fixed_xml)
            ns = {'gpx': root.tag.split('}')[0].strip('{')}
            points = []
            for wpt in root.findall('.//gpx:wpt', ns):
                try:
                    lat = float(wpt.attrib.get('lat', 0))
                    lon = float(wpt.attrib.get('lon', 0))
                except (TypeError, ValueError):
                    continue
                name = None
                name_tag = wpt.find('gpx:name', ns)
                if name_tag is not None and name_tag.text:
                    name = name_tag.text
                if name is None:
                    for child in wpt:
                        if child.tag.endswith('meta_name_com') and child.text:
                            name = child.text
                            break
                if name is None:
                    name = f"Point d'eau {lat:.5f},{lon:.5f}"
                points.append({'lat': lat, 'lon': lon, 'name': name})
            with _water_points_lock:
                _water_points_cache = points
            print(f"✅ {len(points)} points d'eau préchargés")
        else:
            print("⚠️ Fichier water.gpx non trouvé")
    except Exception as e:
        print(f"❌ Erreur lors du préchargement des points d'eau: {e}")

def load_restaurants_cache():
    """Précharger les restaurants au démarrage pour optimiser les performances."""
    global _restaurants_cache
    try:
        if os.path.exists(WC_JSON):
            print("Préchargement des restaurants...")
            with open(WC_JSON, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            restaurants = []
            for feature in data.get('features', []):
                try:
                    coords = feature['geometry']['coordinates']
                    lon, lat = coords[0], coords[1]
                    tags = feature['properties']['tags']
                    name = tags.get('name', f"Restaurant {lat:.5f},{lon:.5f}")
                    cuisine = tags.get('cuisine', '')
                    phone = tags.get('phone', '')
                    website = tags.get('website', '')
                    restaurant = {
                        'lat': lat, 
                        'lon': lon, 
                        'name': name,
                        'cuisine': cuisine,
                        'phone': phone,
                        'website': website
                    }
                    restaurants.append(restaurant)
                except (KeyError, IndexError, TypeError):
                    continue
            with _restaurants_lock:
                _restaurants_cache = restaurants
            print(f"✅ {len(restaurants)} restaurants préchargés")
        else:
            print("⚠️ Fichier wc.json non trouvé")
    except Exception as e:
        print(f"❌ Erreur lors du préchargement des restaurants: {e}")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max

processing_status = {}
status_lock = threading.Lock()

load_existing_pages()
load_water_points_cache()
load_restaurants_cache()


def set_status(task_id, progress, message, done=False, page_url=None, error=None):
    with status_lock:
        processing_status[task_id] = {
            'progress': progress,
            'message': message,
            'done': done,
            'page_url': page_url,
            'error': error,
        }


def get_status(task_id):
    with status_lock:
        return processing_status.get(task_id, None)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def unique_name(base_name, folder, extension):
    safe_base = secure_filename(base_name)
    candidate = f"{safe_base}.{extension}"
    count = 1
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{safe_base}-{count}.{extension}"
        count += 1
    return os.path.splitext(candidate)[0]


def haversine(lat1, lon1, lat2, lon2):
    try:
        r = 6371000.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    except (TypeError, ValueError):
        return float('inf')


def point_to_segment_distance(lat, lon, lat1, lon1, lat2, lon2):
    if lat1 == lat2 and lon1 == lon2:
        return haversine(lat, lon, lat1, lon1)
    mean_lat = math.radians((lat + lat1 + lat2) / 3.0)
    x = (lon2 - lon1) * math.cos(mean_lat)
    y = lat2 - lat1
    dx = (lon - lon1) * math.cos(mean_lat)
    dy = lat - lat1
    proj = (dx * x + dy * y) / (x * x + y * y)
    if proj < 0:
        return haversine(lat, lon, lat1, lon1)
    if proj > 1:
        return haversine(lat, lon, lat2, lon2)
    closest_lon = lon1 + proj * (lon2 - lon1)
    closest_lat = lat1 + proj * (lat2 - lat1)
    return haversine(lat, lon, closest_lat, closest_lon)


def parse_gpx_track_points(gpx_path):
    tree = ET.parse(gpx_path)
    root = tree.getroot()
    ns = {'gpx': root.tag.split('}')[0].strip('{')}
    points = []
    for trkseg in root.findall('.//gpx:trkseg', ns):
        for trkpt in trkseg.findall('gpx:trkpt', ns):
            try:
                lat = float(trkpt.attrib.get('lat', 0))
                lon = float(trkpt.attrib.get('lon', 0))
            except (TypeError, ValueError):
                continue
            points.append((lat, lon))
    return points


def parse_water_points():
    """Retourne les points d'eau depuis le cache préchargé."""
    with _water_points_lock:
        if _water_points_cache is not None:
            return _water_points_cache.copy()  # Retourner une copie pour éviter les modifications
        else:
            # Fallback si le cache n'est pas chargé (ne devrait pas arriver en production)
            print("⚠️ Cache des points d'eau non disponible, parsing à la volée...")
            if not os.path.exists(WATER_GPX):
                return []
            with open(WATER_GPX, 'r', encoding='utf-8', errors='replace') as f:
                raw_xml = f.read()
            fixed_xml = re.sub(r'&(?!([A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)', '&amp;', raw_xml)
            root = ET.fromstring(fixed_xml)
            ns = {'gpx': root.tag.split('}')[0].strip('{')}
            points = []
            for wpt in root.findall('.//gpx:wpt', ns):
                try:
                    lat = float(wpt.attrib.get('lat', 0))
                    lon = float(wpt.attrib.get('lon', 0))
                except (TypeError, ValueError):
                    continue
                name = None
                name_tag = wpt.find('gpx:name', ns)
                if name_tag is not None and name_tag.text:
                    name = name_tag.text
                if name is None:
                    for child in wpt:
                        if child.tag.endswith('meta_name_com') and child.text:
                            name = child.text
                            break
                if name is None:
                    name = f"Point d'eau {lat:.5f},{lon:.5f}"
                points.append({'lat': lat, 'lon': lon, 'name': name})
            return points


def parse_restaurants():
    """Retourne les restaurants depuis le cache préchargé."""
    with _restaurants_lock:
        if _restaurants_cache is not None:
            return _restaurants_cache.copy()  # Retourner une copie pour éviter les modifications
        else:
            # Fallback si le cache n'est pas chargé
            print("⚠️ Cache des restaurants non disponible, parsing à la volée...")
            if not os.path.exists(WC_JSON):
                return []
            try:
                with open(WC_JSON, 'r', encoding='utf-8', errors='replace') as f:
                    data = json.load(f)
                restaurants = []
                for feature in data.get('features', []):
                    try:
                        coords = feature['geometry']['coordinates']
                        lon, lat = coords[0], coords[1]
                        tags = feature['properties']['tags']
                        name = tags.get('name', f"Restaurant {lat:.5f},{lon:.5f}")
                        cuisine = tags.get('cuisine', '')
                        phone = tags.get('phone', '')
                        website = tags.get('website', '')
                        restaurant = {
                            'lat': lat, 
                            'lon': lon, 
                            'name': name,
                            'cuisine': cuisine,
                            'phone': phone,
                            'website': website
                        }
                        restaurants.append(restaurant)
                    except (KeyError, IndexError, TypeError):
                        continue
                return restaurants
            except Exception as e:
                print(f"⚠️ Erreur lecture restaurants: {e}")
                return []


def nearby_water_points(track_points, max_distance_m=500, task_id=None):
    water_points = parse_water_points()
    nearby = []
    if not track_points:
        return nearby
    
    # Optimisation 1 : calcul de la bounding box de la trace + padding
    lats = [tp[0] for tp in track_points]
    lons = [tp[1] for tp in track_points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    # Padding approximatif : ~0.006° ≈ 500m (varie selon latitude)
    lat_padding = 0.01
    lon_padding = 0.01
    bbox_min_lat = min_lat - lat_padding
    bbox_max_lat = max_lat + lat_padding
    bbox_min_lon = min_lon - lon_padding
    bbox_max_lon = max_lon + lon_padding
    
    # Optimisation 2 : filtrer les points d'eau hors bbox
    water_in_bbox = [p for p in water_points 
                     if bbox_min_lat <= p['lat'] <= bbox_max_lat 
                     and bbox_min_lon <= p['lon'] <= bbox_max_lon]
    
    # Optimisation 3 : échantillonner la trace (1 point tous les ~200 au lieu de 1000)
    sample_step = max(1, len(track_points) // 300)
    sampled_points = track_points[::sample_step]
    
    total = len(water_in_bbox)
    progress_step = max(1, total // 100)
    for i, point in enumerate(water_in_bbox, start=1):
        lat = point['lat']
        lon = point['lon']
        try:
            min_dist = min(haversine(lat, lon, float(tp[0]), float(tp[1])) for tp in sampled_points)
        except (TypeError, ValueError):
            continue
        if min_dist <= max_distance_m:
            point_copy = point.copy()
            point_copy['distance_m'] = int(min_dist)
            nearby.append(point_copy)
        if task_id and total > 100 and i % progress_step == 0:
            progress = 25 + int(i / total * 60)
            set_status(task_id, progress, f"Traitement {i}/{total} points")
    return nearby


def nearby_restaurants(track_points, max_distance_m=500, task_id=None):
    restaurants = parse_restaurants()
    nearby = []
    if not track_points:
        return nearby
    
    # Optimisation 1 : calcul de la bounding box de la trace + padding
    lats = [tp[0] for tp in track_points]
    lons = [tp[1] for tp in track_points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    lat_padding = 0.01
    lon_padding = 0.01
    bbox_min_lat = min_lat - lat_padding
    bbox_max_lat = max_lat + lat_padding
    bbox_min_lon = min_lon - lon_padding
    bbox_max_lon = max_lon + lon_padding
    
    # Optimisation 2 : filtrer les restaurants hors bbox
    restaurants_in_bbox = [p for p in restaurants 
                           if bbox_min_lat <= p['lat'] <= bbox_max_lat 
                           and bbox_min_lon <= p['lon'] <= bbox_max_lon]
    
    # Optimisation 3 : échantillonner la trace
    sample_step = max(1, len(track_points) // 300)
    sampled_points = track_points[::sample_step]
    
    total = len(restaurants_in_bbox)
    progress_step = max(1, total // 100)
    for i, point in enumerate(restaurants_in_bbox, start=1):
        lat = point['lat']
        lon = point['lon']
        try:
            min_dist = min(haversine(lat, lon, float(tp[0]), float(tp[1])) for tp in sampled_points)
        except (TypeError, ValueError):
            continue
        if min_dist <= max_distance_m:
            point_copy = point.copy()
            point_copy['distance_m'] = int(min_dist)
            nearby.append(point_copy)
        if task_id and total > 100 and i % progress_step == 0:
            progress = 85 + int(i / total * 10)
            set_status(task_id, progress, f"Traitement restaurants {i}/{total}")
    return nearby



def process_gpx_task(task_id, upload_path, page_name, original_name):
    try:
        set_status(task_id, 5, 'Analyse du fichier GPX...')
        track_points = parse_gpx_track_points(upload_path)
        set_status(task_id, 15, 'Utilisation des points d\u2019eau préchargés...')
        water_points = parse_water_points()
        set_status(task_id, 25, f'{len(water_points)} points d\u2019eau disponibles')
        nearby_water = nearby_water_points(track_points, max_distance_m=500, task_id=task_id)
        set_status(task_id, 80, 'Recherche des restaurants...')
        nearby_restaurants_list = nearby_restaurants(track_points, max_distance_m=500, task_id=task_id)
        
        # Déduplication : si un restaurant est au même endroit qu'un point d'eau, supprimer le point d'eau
        # Seuil de proximité : 10 mètres (coordonnées très proches)
        restaurant_locations = set()
        for restaurant in nearby_restaurants_list:
            restaurant_locations.add((round(restaurant['lat'], 5), round(restaurant['lon'], 5)))
        
        nearby_water_filtered = [w for w in nearby_water 
                                 if (round(w['lat'], 5), round(w['lon'], 5)) not in restaurant_locations]
        
        set_status(task_id, 85, f'{len(nearby_water_filtered)} points d\u2019eau et {len(nearby_restaurants_list)} restaurants trouvés')
        set_status(task_id, 90, 'Génération de la page...')

        page_path = os.path.join(PAGE_FOLDER, f"{page_name}.html")
        page_name_html = html.escape(page_name)
        original_name_html = html.escape(original_name)
        gpx_url = f"/uploads/{page_name}.gpx"
        water_points_js = ','.join([
            '{{lat:{lat},lon:{lon},name:"{name}",distance:{distance}}}'.format(
                lat=pt['lat'], lon=pt['lon'], name=html.escape(pt['name']).replace('"', '\\"'), distance=pt['distance_m'])
            for pt in nearby_water_filtered
        ])
        restaurants_js = ','.join([
            '{{lat:{lat},lon:{lon},name:"{name}",cuisine:"{cuisine}",distance:{distance}}}'.format(
                lat=pt['lat'], lon=pt['lon'], name=html.escape(pt['name']).replace('"', '\\"'), 
                cuisine=html.escape(pt.get('cuisine', '')).replace('"', '\\"'), distance=pt['distance_m'])
            for pt in nearby_restaurants_list
        ])
        if nearby_water_filtered:
            water_list_html = '<ul>' + ''.join([
                f"<li>{html.escape(pt['name'])} — {pt['distance_m']} m</li>"
                for pt in nearby_water_filtered
            ]) + '</ul>'
        else:
            water_list_html = '<div class="empty-message">✨ Aucun point d\u2019eau trouvé sous 500 m du parcours.</div>'
        if nearby_restaurants_list:
            restaurants_list_html = '<ul>' + ''.join([
                f"<li>{html.escape(pt['name'])}" + 
                (f" ({html.escape(pt['cuisine'])})" if pt.get('cuisine') else "") + 
                f" — {pt['distance_m']} m</li>"
                for pt in nearby_restaurants_list
            ]) + '</ul>'
        else:
            restaurants_list_html = '<div class="empty-message">✨ Aucun restaurant trouvé sous 500 m du parcours.</div>'

        with open(page_path, 'w', encoding='utf-8') as f:
            f.write(f"""<!doctype html>
<html lang=\"fr\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{page_name_html}</title>
  <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" />
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      min-height: 100vh;
      padding: 20px;
    }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
    }}
    nav {{
      background: rgba(255, 255, 255, 0.95);
      padding: 15px 20px;
      border-radius: 8px;
      margin-bottom: 20px;
      box-shadow: 0 5px 20px rgba(0, 0, 0, 0.1);
    }}
    nav a {{
      color: #667eea;
      text-decoration: none;
      font-weight: 600;
      transition: color 0.2s;
    }}
    nav a:hover {{
      color: #764ba2;
    }}
    h1 {{
      color: white;
      margin-bottom: 10px;
      font-size: 2rem;
    }}
    .header-info {{
      color: rgba(255, 255, 255, 0.9);
      font-size: 0.95rem;
      margin-bottom: 20px;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 30px;
      box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
      margin-bottom: 30px;
    }}
    #map {{
      width: 100%;
      height: 500px;
      border-radius: 8px;
      margin-bottom: 20px;
    }}
    h2 {{
      color: #333;
      margin-bottom: 15px;
      font-size: 1.3rem;
    }}
    section ul {{
      list-style: none;
      padding: 0;
    }}
    section li {{
      padding: 12px;
      margin-bottom: 8px;
      background: #f8f9fa;
      border-left: 4px solid #667eea;
      border-radius: 4px;
      color: #333;
    }}
    .empty-message {{
      padding: 20px;
      background: #f0f4ff;
      border-radius: 8px;
      color: #667eea;
      border-left: 4px solid #667eea;
    }}
    @media (max-width: 768px) {{
      .container {{ padding: 0; }}
      h1 {{ font-size: 1.5rem; }}
      .card {{ padding: 20px; }}
      #map {{ height: 300px; }}
    }}
  </style>
</head>
<body>
  <nav><a href=\"/\">← Retour à la liste</a></nav>
  <div class=\"container\">
    <h1>🗺️ {page_name_html}</h1>
    <div class=\"header-info\">Fichier : {original_name_html}</div>
    <div class=\"card\">
      <div id=\"map\"></div>
    </div>
    <div class=\"card\">
      <h2>💧 Points d'eau à moins de 500 m</h2>
      <section>
        {water_list_html}
      </section>
    </div>
    <div class=\"card\">
      <h2>🍽️ Restaurants à moins de 500 m</h2>
      <section>
        {restaurants_list_html}
      </section>
    </div>
  </div>
  <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>
  <script src=\"https://cdnjs.cloudflare.com/ajax/libs/leaflet-gpx/1.7.0/gpx.min.js\"></script>
  <script>
    const map = L.map('map').setView([0, 0], 2);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    const waterPoints = [{water_points_js}];
    const waterLayer = L.layerGroup().addTo(map);
    const waterIcon = L.icon({{
      iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png',
      iconSize: [25, 41],
      iconAnchor: [12, 41],
      popupAnchor: [1, -34],
      shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
      shadowSize: [41, 41]
    }});

    const restaurantPoints = [{restaurants_js}];
    const restaurantLayer = L.layerGroup().addTo(map);
    const restaurantIcon = L.icon({{
      iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2cadd.png',
      iconSize: [25, 41],
      iconAnchor: [12, 41],
      popupAnchor: [1, -34],
      shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
      shadowSize: [41, 41],
      className: 'restaurant-marker'
    }});

    waterPoints.forEach(function(point) {{
      const marker = L.marker([point.lat, point.lon], {{ icon: waterIcon }})
        .bindPopup(
          '<strong>' + point.name + '</strong><br>' +
          'Distance: ' + point.distance + ' m<br>' +
          '<a href=\"https://maps.google.com/maps?q=&layer=c&cbll=' + point.lat + ',' + point.lon + '\" target=\"_blank\">Street View</a>'
        );
      marker.addTo(waterLayer);
    }});

    restaurantPoints.forEach(function(point) {{
      const marker = L.marker([point.lat, point.lon], {{ icon: restaurantIcon }})
        .bindPopup(
          '<strong>' + point.name + '</strong><br>' +
          (point.cuisine ? 'Cuisine: ' + point.cuisine + '<br>' : '') +
          'Distance: ' + point.distance + ' m<br>' +
          '<a href=\"https://maps.google.com/maps?q=&layer=c&cbll=' + point.lat + ',' + point.lon + '\" target=\"_blank\">Street View</a>'
        );
      marker.addTo(restaurantLayer);
    }});

    new L.GPX('{gpx_url}', {{async: true}}).on('loaded', function(e) {{
      map.fitBounds(e.target.getBounds());
      const allMarkers = [];
      if (waterPoints.length || restaurantPoints.length) {{
        if (waterLayer.getBounds().isValid()) {{
          allMarkers.push(waterLayer.getBounds());
        }}
        if (restaurantLayer.getBounds().isValid()) {{
          allMarkers.push(restaurantLayer.getBounds());
        }}
      }}
      if (allMarkers.length) {{
        const combinedBounds = allMarkers[0];
        for (let i = 1; i < allMarkers.length; i++) {{
          combinedBounds.extend(allMarkers[i]);
        }}
        map.fitBounds(combinedBounds.extend(e.target.getBounds()));
      }}
    }}).addTo(map);
  </script>
  <style>
    .restaurant-marker {{
      filter: hue-rotate(10deg) brightness(1.2);
    }}
  </style>
</body>
</html>""")

        set_status(task_id, 100, 'Terminé', done=True, page_url=f'/generated/{page_name}')
    except Exception as e:
        set_status(task_id, 100, f'Erreur : {e}', done=True, error=str(e))


@app.route('/')
def home():
    pages = []
    for filename in sorted(os.listdir(PAGE_FOLDER)):
        if filename.endswith('.html'):
            pages.append({'name': filename[:-5]})
    return render_template('index.html', pages=pages)


@app.route('/upload', methods=['POST'])
def upload():
    if 'gpx_file' not in request.files:
        return jsonify({'error': 'Aucun fichier envoyé'}), 400

    file = request.files['gpx_file']
    if file.filename == '' or not allowed_file(file.filename):
        return jsonify({'error': 'Fichier non valide'}), 400

    page_name_input = request.form.get('page_name', '').strip()
    if not page_name_input:
        return jsonify({'error': 'Le nom de la page est requis'}), 400

    # Sécuriser le nom et vérifier l'unicité
    page_name = secure_filename(page_name_input)
    if not page_name:
        return jsonify({'error': 'Le nom contient des caractères invalides'}), 400

    # Si le fichier existe déjà, on le numéro
    page_path = os.path.join(PAGE_FOLDER, f"{page_name}.html")
    if os.path.exists(page_path):
        count = 1
        while os.path.exists(os.path.join(PAGE_FOLDER, f"{page_name}-{count}.html")):
            count += 1
        page_name = f"{page_name}-{count}"

    upload_path = os.path.join(UPLOAD_FOLDER, f"{page_name}.gpx")
    file.save(upload_path)

    task_id = str(uuid.uuid4())
    set_status(task_id, 0, 'Fichier reçu, préparation du traitement...')
    thread = threading.Thread(target=process_gpx_task, args=(task_id, upload_path, page_name, file.filename))
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id}), 202


@app.route('/status/<task_id>')
def status(task_id):
    status_data = get_status(task_id)
    if status_data is None:
        return jsonify({'error': 'Tâche introuvable'}), 404
    return jsonify(status_data)


@app.route('/generated/<page_name>')
def page(page_name):
    filename = secure_filename(f"{page_name}.html")
    return send_from_directory(PAGE_FOLDER, filename)


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    safe_filename = secure_filename(filename)
    return send_from_directory(UPLOAD_FOLDER, safe_filename)


@app.route('/delete/<page_name>', methods=['POST'])
def delete(page_name):
    safe_name = secure_filename(page_name)
    page_path = os.path.join(PAGE_FOLDER, f"{safe_name}.html")
    upload_path = os.path.join(UPLOAD_FOLDER, f"{safe_name}.gpx")

    if os.path.exists(page_path):
        os.remove(page_path)
    if os.path.exists(upload_path):
        os.remove(upload_path)

    return redirect(url_for('home'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)

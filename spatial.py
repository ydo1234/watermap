"""Spherical distances to the complete route, pruned with a bounding-volume tree."""
import math

EARTH_RADIUS = 6_371_000.0


def vector(lat, lon):
    lat, lon = math.radians(lat), math.radians(lon)
    return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def angle(a, b):
    return math.atan2(math.sqrt(dot(cross(a, b), cross(a, b))), dot(a, b))


def arc_distance(point, start, end):
    normal = cross(start, end)
    norm = math.sqrt(dot(normal, normal))
    if norm < 1e-12:
        return min(angle(point, start), angle(point, end)) * EARTH_RADIUS
    normal = tuple(x / norm for x in normal)
    height = dot(point, normal)
    projection = tuple(p - height*n for p, n in zip(point, normal))
    if dot(cross(start, projection), normal) >= -1e-14 and dot(cross(projection, end), normal) >= -1e-14:
        return math.asin(min(1.0, abs(height))) * EARTH_RADIUS
    return min(angle(point, start), angle(point, end)) * EARTH_RADIUS


def bounds_distance(point, bounds):
    return sum(max(lo-p, 0.0, p-hi)**2 for p, (lo, hi) in zip(point, bounds))


class PointSet(tuple):
    """Reference coordinates transformed once at startup, shared by all jobs."""
    def __new__(cls, points):
        instance = super().__new__(cls, points)
        instance.vectors = tuple(vector(p['lat'], p['lon']) for p in instance)
        return instance


class RouteIndex:
    """BVH over minor great-circle arcs; no route sampling or latitude assumptions."""
    def __init__(self, track):
        vertices = [vector(*point) for point in track]
        breaks = getattr(track, 'segment_starts', set())
        segments = []
        for index, start in enumerate(vertices):
            end = vertices[index+1] if index+1 < len(vertices) and index+1 not in breaks else start
            # Every point on the arc lies within this padded endpoint box.
            padding = 1.0 - math.cos(angle(start, end) / 2.0) + 1e-14
            bounds = tuple((min(a, b)-padding, max(a, b)+padding) for a, b in zip(start, end))
            segments.append((bounds, start, end))
        self.root = self._build(segments) if segments else None
        self.checks = 0

    def _build(self, segments):
        bounds = tuple((min(s[0][i][0] for s in segments), max(s[0][i][1] for s in segments)) for i in range(3))
        if len(segments) <= 8:
            return (bounds, segments, None, None)
        axis = max(range(3), key=lambda i: bounds[i][1]-bounds[i][0])
        segments.sort(key=lambda s: sum(s[0][axis]))
        middle = len(segments)//2
        return (bounds, None, self._build(segments[:middle]), self._build(segments[middle:]))

    def distance(self, lat, lon, maximum):
        return self.distance_vector(vector(lat, lon), maximum)

    def distance_vector(self, point, maximum):
        if not self.root:
            return math.inf
        best = maximum + 1e-7
        found = math.inf
        nodes = [self.root]
        while nodes:
            bounds, segments, left, right = nodes.pop()
            chord = 2.0 * math.sin(best / (2.0 * EARTH_RADIUS))
            if bounds_distance(point, bounds) > chord*chord:
                continue
            if segments is None:
                # Visit the nearer child first so its result tightens the bound.
                children = sorted((left, right), key=lambda n: bounds_distance(point, n[0]), reverse=True)
                nodes.extend(children)
                continue
            for segment_bounds, start, end in segments:
                self.checks += 1
                if self.checks > 5_000_000:
                    raise ValueError('Ce tracé est trop complexe. Découpez-le en plusieurs itinéraires.')
                if bounds_distance(point, segment_bounds) > chord*chord:
                    continue
                distance = arc_distance(point, start, end)
                if distance <= best:
                    best = found = distance
        return found

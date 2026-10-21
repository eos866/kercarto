"""Génère des plans de lotissement de démonstration (zones, îlots, parcelles, réseaux)
pour les 6 lotissements communaux fictifs de Kër Carto, posés hors des contraintes OSM
(zones humides, aires protégées, zones bâties, industrielles, militaires, cimetières,
voies ferrées, autoroutes, lignes HT, littoral).

Sortie : sql/*.sql (un fichier par lotissement) prêts à exécuter.
"""
import json, math, random, uuid, os
import numpy as np

random.seed(2026)
HERE = os.path.dirname(os.path.abspath(__file__))
FEAT = json.load(open(os.path.join(HERE, 'feat.json')))['rows']

SITES = {
    # clé: (program_id, nom, lon, lat, tailles de parcelles [(m², poids)], nb parcelles visé, prix par m² de parcelle, contact)
    'km':   ('8f5d7d57-b616-45c4-84ce-a6e8d0440a5c', 'Lotissement Keur Massar Extension', -17.305, 14.79, [(150, .55), (200, .45)], 320, 30000, 'Commune de Keur Massar'),
    'dia':  ('6d455dab-0162-4696-ada8-09c5e88fa533', 'Lotissement Diass Gare', -17.075, 14.66, [(200, .6), (300, .4)], 410, 24500, 'Commune de Diass'),
    'pout': ('e49fd923-0b45-45b1-97bc-53a4d2a34299', 'Lotissement Pout Nord', -17.06, 14.785, [(250, 1.0)], 260, 14000, 'Commune de Pout'),
    'saly': ('d0799340-87c3-4245-8cac-141cf64e369c', 'Lotissement Saly Carrefour', -16.985, 14.455, [(200, .5), (300, .5)], 140, 44000, 'Commune de Saly-Portudal'),
    'sang': ('6a3552e5-50f5-4c97-9bc0-d57250155135', 'Lotissement Sangalkam Village', -17.225, 14.775, [(150, 1.0)], 180, 26000, 'Commune de Sangalkam'),
    'zig':  ('4d1d3113-6bb9-4d63-9bd5-20fdc89091ca', 'Lotissement Ziguinchor Kandialang', -16.235, 12.548, [(200, 1.0)], 220, 12000, 'Commune de Ziguinchor'),
}
# Dimensions (façade × profondeur) par surface
DIMS = {150: (10, 15), 200: (10, 20), 250: (12.5, 20), 300: (15, 20)}
ROAD_MAIN, ROAD_SEC, ROAD_PERIM = 20.0, 12.0, 10.0
POLY_BLOCK = {'wetland', 'mangrove', 'protected', 'lu_industrial', 'lu_military', 'lu_cemetery',
              'lu_residential', 'lu_construction', 'aerodrome', 'oil'}
LINE_BUF = {'motorway': 60, 'railway': 40, 'power_line': 40, 'coastline': 1200}
STATUSES = ['disponible immédiatement', 'potentiellement disponible', 'disponible à terme',
            'réservée', 'attribuée', 'non disponible']


class Proj:
    def __init__(self, lon0, lat0):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = 111320 * math.cos(math.radians(lat0))
        self.ky = 110540

    def fwd(self, lon, lat):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def inv(self, x, y):
        return (round(self.lon0 + x / self.kx, 7), round(self.lat0 + y / self.ky, 7))


def rings_of(geom):
    t = geom['type']; c = geom['coordinates']
    if t == 'Polygon':
        return [c[0]]
    if t == 'MultiPolygon':
        return [p[0] for p in c]
    return []


def lines_of(geom):
    t = geom['type']; c = geom['coordinates']
    if t == 'LineString':
        return [c]
    if t == 'MultiLineString':
        return c
    if t in ('Polygon', 'MultiPolygon'):
        return rings_of(geom)
    return []


def pip(px, py, ring):
    """vectorised point in polygon; ring as (N,2) array"""
    x, y = ring[:, 0], ring[:, 1]
    xj, yj = np.roll(x, 1), np.roll(y, 1)
    inside = np.zeros(px.shape, bool)
    for i in range(len(x)):
        cond = ((y[i] > py) != (yj[i] > py)) & (px < (xj[i] - x[i]) * (py - y[i]) / ((yj[i] - y[i]) or 1e-12) + x[i])
        inside ^= cond
    return inside


def seg_dist(px, py, line):
    d = np.full(px.shape, np.inf)
    for (ax, ay), (bx, by) in zip(line[:-1], line[1:]):
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / (L or 1e-12), 0, 1)
        d = np.minimum(d, np.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return d


def site_constraints(site, P):
    polys, lines = [], []
    for f in FEAT:
        if f['site'] != site:
            continue
        g = f['geom'] if isinstance(f['geom'], dict) else json.loads(f['geom'])
        k = f['kind']
        if k in POLY_BLOCK:
            for r in rings_of(g):
                polys.append((k, np.array([P.fwd(*p) for p in r])))
            if k in ('aerodrome', 'oil'):
                for l in lines_of(g):
                    lines.append((k, np.array([P.fwd(*p) for p in l]), 500))
        if k in LINE_BUF:
            for l in lines_of(g):
                lines.append((k, np.array([P.fwd(*p) for p in l]), LINE_BUF[k]))
    return polys, lines


def rect_samples(cx, cy, W, H, ang, step=25):
    us = np.arange(-W / 2, W / 2 + 1, step); vs = np.arange(-H / 2, H / 2 + 1, step)
    U, V = np.meshgrid(us, vs); U, V = U.ravel(), V.ravel()
    c, s = math.cos(ang), math.sin(ang)
    return cx + U * c - V * s, cy + U * s + V * c


def ok(cx, cy, W, H, ang, polys, lines):
    px, py = rect_samples(cx, cy, W + 60, H + 60, ang)
    for k, r in polys:
        if (r[:, 0].max() < px.min() or r[:, 0].min() > px.max() or r[:, 1].max() < py.min() or r[:, 1].min() > py.max()):
            continue
        if pip(px, py, r).any():
            return False
    for k, l, buf in lines:
        if seg_dist(px, py, l).min() < buf:
            return False
    return True


def place(site, W, H, P):
    polys, lines = site_constraints(site, P)
    best = None
    for rad in range(0, 3001, 150):
        n = max(1, int(2 * math.pi * rad / 150)) if rad else 1
        for i in range(n):
            a = 2 * math.pi * i / n
            cx, cy = rad * math.cos(a), rad * math.sin(a)
            for ang_deg in (0, 20, 40, 60, 80, 100, 120, 140, 160):
                ang = math.radians(ang_deg)
                if ok(cx, cy, W, H, ang, polys, lines):
                    return cx, cy, ang, rad
    raise RuntimeError('no site found for ' + site)


def build(site):
    pid, name, lon0, lat0, sizes, target, pm2, contact = SITES[site]
    P = Proj(lon0, lat0)
    # Dimensionnement de la trame
    avg = sum(s * w for s, w in sizes)
    depth = max(DIMS[s][1] for s, _ in sizes)
    block_depth = 2 * depth
    per_block_len = 8  # parcelles par rangée
    fr_avg = sum(DIMS[s][0] * w for s, w in sizes)
    block_len = per_block_len * fr_avg
    parcels_per_block = 2 * per_block_len
    n_blocks_needed = math.ceil(target / parcels_per_block * 1.12)  # + îlots d'équipements/espaces verts
    # disposition : 2 bandes d'îlots de part et d'autre de la voie principale, k colonnes
    rows_each_side = max(1, round(math.sqrt(n_blocks_needed / 2 / 2.2)))
    cols = math.ceil(n_blocks_needed / (2 * rows_each_side))
    W = cols * block_len + (cols + 1) * ROAD_SEC
    H = 2 * rows_each_side * block_depth + (2 * rows_each_side - 2) * ROAD_SEC + ROAD_MAIN + 2 * ROAD_PERIM
    cx, cy, ang, moved = place(site, W, H, P)
    c, s = math.cos(ang), math.sin(ang)
    to_ll = lambda u, v: P.inv(cx + u * c - v * s, cy + u * s + v * c)
    rect_ll = lambda u0, v0, u1, v1: [to_ll(u0, v0), to_ll(u1, v0), to_ll(u1, v1), to_ll(u0, v1), to_ll(u0, v0)]

    sub_id = str(uuid.uuid4())
    blocks, parcels, nets = [], [], []
    # Ordonnées des rangées d'îlots
    v_rows = []
    v = ROAD_MAIN / 2
    for r in range(rows_each_side):
        v_rows.append((v, v + block_depth, r)); v += block_depth + ROAD_SEC
    v_rows = [(-b, -a, -(r + 1)) for a, b, r in v_rows[::-1]] + [(a, b, r + 1) for a, b, r in v_rows]
    u_cols = []
    u = -W / 2 + ROAD_SEC
    for k in range(cols):
        u_cols.append((u, u + block_len, k)); u += block_len + ROAD_SEC
    # Îlots réservés : 1 espace vert, 1 équipement (au centre), phase 2 = colonnes de droite
    centre_col = cols // 2
    special = {(centre_col, 1): 'équipement', (centre_col, -1): 'espace vert'}
    if cols >= 4:
        special[(0, -rows_each_side)] = 'espace vert'
    phase2_from = math.ceil(cols * 0.6)
    lot_no = 0
    bnum = 0
    rng = random.Random(sum(map(ord, site)) * 7919)
    for (u0, u1, k) in u_cols:
        for (v0, v1, r) in v_rows:
            if lot_no >= target and (k, r) not in special:
                continue
            bnum += 1
            usage_b = special.get((k, r), 'habitation')
            b_id = str(uuid.uuid4())
            blocks.append(dict(id=b_id, num=str(bnum), usage=usage_b, geom={'type': 'Polygon', 'coordinates': [rect_ll(u0, v0, u1, v1)]}))
            if usage_b != 'habitation':
                continue
            phase = 2 if k >= phase2_from else 1
            # deux rangées dos à dos ; la rangée côté voie principale
            for half in (0, 1):
                va, vb = (v0, v0 + depth) if half == 0 else (v1 - depth, v1)
                near_main = (abs(va) <= ROAD_MAIN / 2 + 1) or (abs(vb) <= ROAD_MAIN / 2 + 1)
                uu = u0
                while uu < u1 - 5 and lot_no < target:
                    sz = rng.choices([s_ for s_, _ in sizes], [w for _, w in sizes])[0]
                    f, d = DIMS[sz]
                    if uu + f > u1 + 0.01:
                        break
                    lot_no += 1
                    a = round(f * (vb - va))
                    usage = 'commerce' if near_main and rng.random() < 0.45 else ('mixte' if near_main else 'habitation')
                    # statut : phase 2 surtout « à terme », sinon majorité attribuée
                    if phase == 2:
                        st = rng.choices(STATUSES, [8, 10, 62, 8, 7, 5])[0]
                    else:
                        st = rng.choices(STATUSES, [18, 8, 0, 11, 56, 7])[0]
                    price_m2 = pm2 * (1.35 if usage == 'commerce' else 1.15 if usage == 'mixte' else 1.0)
                    price = round(a * price_m2 / 50000) * 50000
                    # raccordements
                    d_main = max(0.0, min(abs(va), abs(vb)) - ROAD_MAIN / 2)
                    def rc(state, dist=None):
                        o = {'etat': state}
                        if dist is not None:
                            o['distance_m'] = int(round(dist))
                        return o
                    racc = {
                        'voirie': rc('en limite de lot', 0),
                        'eau': rc('en limite de lot', 0) if phase == 1 else rc('programmé'),
                        'electricite': rc('en limite de lot', 0) if phase == 1 else rc('programmé'),
                        'assainissement': rc('programmé') if (phase == 1 and d_main < 60) else rc('non disponible'),
                        'fibre': rc('en limite de lot', 0) if near_main else (rc('à moins de 100 m', d_main) if d_main < 100 else rc('programmé')),
                    }
                    cu, cv = uu + f / 2, (va + vb) / 2
                    clon, clat = to_ll(cu, cv)
                    parcels.append(dict(id=str(uuid.uuid4()), block_id=b_id, block_num=str(bnum), lot_num=lot_no,
                                        ref=f'Îlot {bnum} – Lot {lot_no}', usage=usage, area_m2=a, frontage_m=f, depth_m=vb - va,
                                        price=price, price_m2=round(price / a), status=st, raccordements=racc, phase=phase,
                                        lon=clon, lat=clat,
                                        geom={'type': 'Polygon', 'coordinates': [rect_ll(uu, va, uu + f, vb)]}))
                    uu += f
    # Réseaux
    um0, um1 = -W / 2, W / 2
    p2u = u_cols[phase2_from][0] - ROAD_SEC / 2 if phase2_from < cols else um1
    line = lambda pts: {'type': 'LineString', 'coordinates': [to_ll(*p) for p in pts]}
    nets.append(dict(kind='voirie_principale', label=f'Voie principale ({int(ROAD_MAIN)} m)', geom=line([(um0, 0), (um1, 0)])))
    for (u0, u1, k) in u_cols:
        x = u0 - ROAD_SEC / 2
        nets.append(dict(kind='voirie_secondaire', label=f'Voie secondaire ({int(ROAD_SEC)} m)', geom=line([(x, -H / 2 + ROAD_PERIM / 2), (x, H / 2 - ROAD_PERIM / 2)])))
    x = u_cols[-1][1] + ROAD_SEC / 2
    nets.append(dict(kind='voirie_secondaire', label=f'Voie secondaire ({int(ROAD_SEC)} m)', geom=line([(x, -H / 2 + ROAD_PERIM / 2), (x, H / 2 - ROAD_PERIM / 2)])))
    for (v0, v1, r) in v_rows:
        if abs(r) < rows_each_side:
            y = (v1 + ROAD_SEC / 2) if r > 0 else (v0 - ROAD_SEC / 2)
            nets.append(dict(kind='voirie_secondaire', label=f'Voie secondaire ({int(ROAD_SEC)} m)', geom=line([(um0, y), (um1, y)])))
    for y in (-H / 2 + ROAD_PERIM / 2, H / 2 - ROAD_PERIM / 2):
        nets.append(dict(kind='voirie_secondaire', label=f'Voie de ceinture ({int(ROAD_PERIM)} m)', geom=line([(um0, y), (um1, y)])))
    nets.append(dict(kind='eau', label='Conduite d\'eau potable (réalisée)', geom=line([(um0, 4), (p2u, 4)])))
    nets.append(dict(kind='eau', label='Conduite d\'eau potable (programmée, phase 2)', geom=line([(p2u, 4), (um1, 4)]), etat='programmé'))
    nets.append(dict(kind='electricite_mt', label='Ligne électrique MT (réalisée)', geom=line([(um0, -4), (p2u, -4)])))
    nets.append(dict(kind='electricite_mt', label='Ligne électrique MT (programmée)', geom=line([(p2u, -4), (um1, -4)]), etat='programmé'))
    for (u0, u1, k) in u_cols:
        if k < phase2_from:
            x = u0 - ROAD_SEC / 2 + 2
            nets.append(dict(kind='electricite_bt', label='Réseau BT', geom=line([(x, -H / 2 + 8), (x, H / 2 - 8)])))
            nets.append(dict(kind='eau', label='Réseau de distribution d\'eau', geom=line([(x - 4, -H / 2 + 8), (x - 4, H / 2 - 8)])))
    nets.append(dict(kind='assainissement', label='Collecteur d\'assainissement (programmé)', geom=line([(um0, 7), (p2u, 7)]), etat='programmé'))
    nets.append(dict(kind='fibre', label='Fibre optique', geom=line([(um0, -7), (um1, -7)])))
    perim = rect_ll(-W / 2, -H / 2, W / 2, H / 2)
    lons = [p[0] for p in perim]; lats = [p[1] for p in perim]
    clon, clat = to_ll(0, 0)
    counts = {s_: sum(1 for p in parcels if p['status'] == s_) for s_ in STATUSES}
    sub = dict(id=sub_id, program_id=pid, name=name, contact=contact, area_m2=round(W * H), parcel_count=len(parcels),
               perimeter={'type': 'Polygon', 'coordinates': [perim]}, minlon=min(lons), minlat=min(lats), maxlon=max(lons), maxlat=max(lats),
               lon=clon, lat=clat, moved_m=moved, ang=round(math.degrees(ang)), W=round(W), H=round(H), counts=counts,
               viabilisation={'voirie': 'réalisée', 'eau': 'en cours', 'electricite': 'en cours', 'assainissement': 'programmée', 'fibre': 'programmée'})
    return sub, blocks, parcels, nets


def q(v):
    if v is None:
        return 'null'
    if isinstance(v, (dict, list)):
        return "'" + json.dumps(v, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def to_sql(sub, blocks, parcels, nets):
    out = []
    out.append(f"delete from subdivisions where program_id = {q(sub['program_id'])};")
    out.append(
        "insert into subdivisions (id, program_id, name, maitre_ouvrage, legal_status, area_m2, parcel_count, viabilisation, perimeter, minlon, minlat, maxlon, maxlat, lon, lat, is_demo) values "
        f"({q(sub['id'])}, {q(sub['program_id'])}, {q(sub['name'])}, {q(sub['contact'])}, 'approuvé', {sub['area_m2']}, {sub['parcel_count']}, {q(sub['viabilisation'])}, {q(sub['perimeter'])}, "
        f"{sub['minlon']}, {sub['minlat']}, {sub['maxlon']}, {sub['maxlat']}, {sub['lon']}, {sub['lat']}, true);")
    for i in range(0, len(blocks), 200):
        vals = ",".join(f"({q(b['id'])}, {q(sub['id'])}, {q(b['num'])}, {q(b['usage'])}, {q(b['geom'])})" for b in blocks[i:i + 200])
        out.append(f"insert into blocks (id, subdivision_id, num, usage, geom) values {vals};")
    for i in range(0, len(parcels), 150):
        vals = ",".join(
            f"({q(p['id'])}, {q(sub['id'])}, {q(p['block_id'])}, {q(p['ref'])}, {p['lot_num']}, {q(p['usage'])}, {p['area_m2']}, {p['frontage_m']}, {p['depth_m']}, {p['price']}, {p['price_m2']}, {q(p['status'])}, {q(p['raccordements'])}, {p['phase']}, {p['lon']}, {p['lat']}, {q(p['geom'])}, true)"
            for p in parcels[i:i + 150])
        out.append("insert into parcels (id, subdivision_id, block_id, ref, lot_num, usage, area_m2, frontage_m, depth_m, price, price_m2, status, raccordements, phase, lon, lat, geom, is_demo) values " + vals + ";")
    vals = ",".join(f"({q(sub['id'])}, {q(n['kind'])}, {q(n['label'])}, {q(n.get('etat', 'réalisé'))}, {q(n['geom'])})" for n in nets)
    out.append(f"insert into networks (subdivision_id, kind, label, etat, geom) values {vals};")
    avail = sub['counts']['disponible immédiatement']
    out.append(f"update programs set lon = {sub['lon']}, lat = {sub['lat']}, footprint = {q(sub['perimeter'])}, total_units = {sub['parcel_count']}, available_units = {avail}, location_precision = 'approximative' where id = {q(sub['program_id'])};")
    return "\n".join(out)


if __name__ == '__main__':
    os.makedirs(os.path.join(HERE, 'sql'), exist_ok=True)
    summary = {}
    for site in SITES:
        sub, blocks, parcels, nets = build(site)
        open(os.path.join(HERE, 'sql', f'{site}.sql'), 'w').write(to_sql(sub, blocks, parcels, nets))
        summary[site] = dict(name=sub['name'], parcels=len(parcels), blocks=len(blocks), nets=len(nets), area_ha=round(sub['area_m2'] / 1e4, 1),
                             moved_m=sub['moved_m'], ang=sub['ang'], WxH=(sub['W'], sub['H']), counts=sub['counts'], center=(sub['lon'], sub['lat']))
    json.dump(summary, open(os.path.join(HERE, 'summary.json'), 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


# ---------- Export compact (coordonnées locales u, v ; la base reconstruit les géométries) ----------
def build_compact(site):
    """Rejoue build() mais renvoie les paramètres locaux pour un chargement compact."""
    global to_sql
    pid, name, lon0, lat0, sizes, target, pm2, contact = SITES[site]
    sub, blocks, parcels, nets = build(site)
    P = Proj(lon0, lat0)
    cx, cy = P.fwd(sub['lon'], sub['lat'])
    ang = math.radians(sub['ang'])
    c, s = math.cos(ang), math.sin(ang)
    def to_uv(lon, lat):
        x, y = P.fwd(lon, lat); x -= cx; y -= cy
        return (round(x * c + y * s, 1), round(-x * s + y * c, 1))
    code = {'en limite de lot': 'L', 'programmé': 'P', 'non disponible': 'N', 'à moins de 100 m': 'C'}
    def rcode(r):
        out = []
        for k in ('voirie', 'eau', 'electricite', 'assainissement', 'fibre'):
            o = r[k]; t = code[o['etat']]
            if t == 'C':
                t += str(o.get('distance_m', 0))
            out.append(t)
        return ','.join(out)
    bnum = {b['id']: b['num'] for b in blocks}
    B = []
    for b in blocks:
        ring = b['geom']['coordinates'][0]
        (u0, v0), (u1, v1) = to_uv(*ring[0]), to_uv(*ring[2])
        B.append([b['num'], b['usage'], min(u0, u1), min(v0, v1), max(u0, u1), max(v0, v1)])
    Pc = []
    for p in parcels:
        ring = p['geom']['coordinates'][0]
        (u0, v0), (u1, v1) = to_uv(*ring[0]), to_uv(*ring[2])
        Pc.append([p['lot_num'], p['block_num'], p['usage'], p['area_m2'], round(min(u0, u1), 1), round(min(v0, v1), 1), round(max(u0, u1), 1), round(max(v0, v1), 1), p['price'], STATUSES.index(p['status']), p['phase'], rcode(p['raccordements'])])
    N = [[n['kind'], n['label'], n.get('etat', 'réalisé'), [list(to_uv(*pt)) for pt in n['geom']['coordinates']]] for n in nets]
    return dict(program_id=pid, name=name, maitre_ouvrage=contact, lon0=lon0, lat0=lat0, kx=P.kx, ky=P.ky, cx=round(cx, 2), cy=round(cy, 2), ang=sub['ang'],
                W=sub['W'], H=sub['H'], area_m2=sub['area_m2'], viabilisation=sub['viabilisation'], blocks=B, parcels=Pc, networks=N)


if __name__ == '__main__':
    for site in SITES:
        j = build_compact(site)
        open(os.path.join(HERE, 'sql', f'{site}.json'), 'w').write(json.dumps(j, ensure_ascii=False, separators=(',', ':')))

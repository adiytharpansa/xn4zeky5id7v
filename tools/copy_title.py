# Robot penyalin judul untuk GitHub Actions (berjalan di runner, token repo sendiri).
# v2 (29 Okt 2026): header Referer/UA pada unduh langsung, coba-ulang per segmen,
# rotasi kandidat provider antar-percobaan, playlist pendek ditolak saat resolusi, pekerja unduh 10.
# v3 (29 Okt 2026): unggah per episode + resume dari isi repo — progres tidak hangus
# bila run berhenti di tengah (kasus Vinland: 13 ep hilang karena gagal di ep14).
# v4 (29 Okt 2026 malam): ambil playlist LANGSUNG dulu (Referer/UA), proxy Worker hanya
# cadangan — terbukti resolusi kosong massal disebabkan playlist yang wajib lewat proxy
# Worker padahal kuota Workers mati (429), sementara sumber & segmen bisa langsung.
# Masukan lewat env: WID, SLUG_ID, ANILIST_ID, TOTAL_EPS, TITLE_NAME, GH_TOKEN, GH_REPO.
import base64, hashlib, json, os, subprocess, sys, time, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

WORKER = 'https://my-anime-app.adiyth-arpansa.workers.dev'
UA = 'Mozilla/5.0 (Linux; Android 14; SM-A155F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36'
WID = os.environ['WID']; SLUG_ID = os.environ['SLUG_ID']
TOTAL = int(os.environ['TOTAL_EPS']); NAME = os.environ.get('TITLE_NAME', WID)
ANILIST = int(os.environ.get('ANILIST_ID') or 0)
TOKEN = os.environ['GH_TOKEN']; REPO = os.environ['GH_REPO']
OUT = os.path.join(os.getcwd(), 'work', WID)
DIRECT_OK = {'v': None}  # None=belum tahu, True=langsung bisa, False=wajib proxy

def log(*a): print(time.strftime('%H:%M:%S'), *a, flush=True)

def gh(m, p, payload=None, tries=6):
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for t in range(tries):
        r = urllib.request.Request('https://api.github.com' + p, data=data, method=m)
        r.add_header('Accept', 'application/vnd.github+json')
        r.add_header('Authorization', 'Bearer ' + TOKEN)
        r.add_header('X-GitHub-Api-Version', '2022-11-28')
        if data: r.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(r, timeout=300) as resp:
                b = resp.read(); return resp.status, (json.loads(b.decode()) if b else {})
        except urllib.error.HTTPError as e:
            try: last = (e.code, json.loads(e.read().decode() or '{}'))
            except Exception: last = (e.code, {})
            if e.code < 500 and e.code not in (403, 429): return last
        except Exception as e:
            last = (0, {'error': str(e)})
        time.sleep(4)
    return last

def blob_sha(data): return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()

def curl_bytes(url, tries=4, timeout=120, ref=None):
    cmd = ['curl', '-s', '-f', '--max-time', str(timeout), '-A', UA]
    if ref: cmd += ['-H', 'Referer: ' + ref]
    cmd.append(url)
    for t in range(tries):
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and r.stdout: return r.stdout
    return None

def prox(u, ref): return WORKER + '/api/proxy?u=' + urllib.parse.quote(u, safe='') + '&ref=' + urllib.parse.quote(ref, safe='')

def anidap_json(url):
    for t in range(4):
        r = subprocess.run(['curl', '-s', '--max-time', '60', '-A', UA, '-H', 'Accept: application/json',
                            '-H', 'Referer: https://anidap.lol/home', url], capture_output=True, text=True)
        try: return json.loads(r.stdout)
        except Exception: time.sleep(3)
    return None

def parse_variant(master, stream_url, ref):
    """Pilih varian bandwidth tertinggi; kembalikan (teks_playlist, url_dasar, daftar_segmen)."""
    lines = [l.strip() for l in master.splitlines() if l.strip()]
    best, bestbw = None, -1
    for i, l in enumerate(lines):
        if l.startswith('#EXT-X-STREAM-INF'):
            bw = 0
            for part in l.split(','):
                if part.strip().startswith('BANDWIDTH='): bw = int(part.strip().split('=')[1])
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if nxt and not nxt.startswith('#') and bw > bestbw: bestbw, best = bw, nxt
    if best is None:
        pl, pl_base = master, stream_url
    else:
        v_abs = resolve_ref(stream_url, best)
        pl, _ = fetch_playlist(v_abs, ref)
        if not pl: return None
        pl_base = v_abs
    durs, segs = [], []
    pending = None
    for l in pl.splitlines():
        l = l.strip()
        if l.startswith('#EXTINF'): pending = float(l.split(':')[1].rstrip(','))
        elif l and not l.startswith('#'):
            durs.append(pending or 0.0); segs.append(l)
    if not segs: return None
    return (pl, pl_base, durs, segs)

def fetch_segment(s_abs, ref):
    if s_abs.startswith(WORKER): return curl_bytes(s_abs, tries=2)
    data = curl_bytes(s_abs, tries=2, ref=ref)
    if is_ts(data): return data
    alt = curl_bytes(prox(s_abs, ref))
    return alt if is_ts(alt) else data


def fetch_raw_smart(u, ref, tries=2):
    """Ambil URL sumber langsung dulu (dengan Referer), proxy Worker cadangan.
    Kembalikan bytes mentah atau None; penelepon memvalidasi isinya."""
    raw = curl_bytes(u, tries=tries, ref=ref)
    if raw: return raw
    return curl_bytes(prox(u, ref), tries=tries)

def resolve_source(slug_id, num, skip=0):
    srv = anidap_json(f'https://chad.anidap.lol/rest/api/servers?id={urllib.parse.quote(slug_id)}&epNum={num}')
    if not srv: return None
    provs = sorted(srv.get('subProviders') or [], key=lambda p: 0 if p.get('default') else 1)
    cands = []
    for prov in provs:
        s = anidap_json(f'https://chad.anidap.lol/rest/api/sources?id={urllib.parse.quote(slug_id)}&epNum={num}&type=sub&providerId={urllib.parse.quote(prov["id"])}')
        if not s: continue
        ref = (s.get('headers') or {}).get('Referer') or 'https://megaplay.buzz/'
        for x in (s.get('sources') or []):
            if x.get('url'): cands.append((x['url'], ref))
    cands.sort(key=lambda c: 0 if 'nexabloom' in c[0] else 1)
    cands_ok = []
    for u, ref in cands:
        raw = fetch_raw_smart(u, ref, tries=2)
        if not raw or not raw.decode('utf-8', 'replace').lstrip().startswith('#EXTM3U'):
            continue
        master = raw.decode('utf-8', 'replace')
        parsed = parse_variant(master, u, ref)
        if not parsed: continue
        pl, pl_base, durs, segs = parsed
        if 0 < sum(durs) < 600: continue  # playlist pendek (cuplikan), bukan episode utuh
        # Uji segmen pertama & tengah: kandidat hanya diterima bila segmennya
        # benar-benar bisa diambil (playlist saja tidak cukup — kasus AoT).
        ok = True
        for idx in {0, len(segs) // 2}:
            s_abs = resolve_ref(pl_base, segs[idx])
            if not is_ts(fetch_segment(s_abs, ref)): ok = False; break
        if ok: cands_ok.append((pl, pl_base, durs, segs, ref))
    if not cands_ok: return None
    return cands_ok[skip % len(cands_ok)]

def fetch_playlist(url, ref):
    if url.startswith(WORKER):
        raw = curl_bytes(url)
        return (raw.decode('utf-8', 'replace') if raw else None), url
    raw = fetch_raw_smart(url, ref)
    return (raw.decode('utf-8', 'replace') if raw else None), url

def resolve_ref(base_url, line):
    if line.startswith('http'): return line
    if line.startswith('/'): return WORKER + line
    return urllib.parse.urljoin(base_url, line)

def is_ts(data): return bool(data) and len(data) > 188 and data[0] == 0x47

def download_episode(slug_id, num, outdir):
    got = None
    for attempt in range(3):
        got = resolve_source(slug_id, num, skip=attempt)
        if got: break
        log(f'  ep{num}: resolusi kosong (coba {attempt + 1}/3)'); time.sleep(10)
    if not got: return False
    pl, pl_base, durs, segs, referer = got
    os.makedirs(outdir, exist_ok=True)
    def grab(i):
        path = os.path.join(outdir, f'seg-{i:03d}.ts')
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, 'rb') as f: head = f.read(1)
            if head == b'\x47': return True
        s_abs = resolve_ref(pl_base, segs[i])
        for rnd in range(3):
            data = fetch_segment(s_abs, referer)
            if is_ts(data):
                with open(path, 'wb') as f: f.write(data)
                return True
            time.sleep(2 + rnd * 3)
        return False
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(grab, range(len(segs))))
    if not all(results):
        log(f'  ep{num}: segmen gagal {results.count(False)}'); return False
    if sum(durs) < 600:
        log(f'  ep{num}: durasi hanya {sum(durs):.0f} dtk, ditolak'); return False
    # Potongan besar untuk runner: s.d. 45 dtk / 18 MB per berkas.
    groups, cur, cur_dur, cur_size = [], [], 0.0, 0
    for i in range(len(segs)):
        sz = os.path.getsize(os.path.join(outdir, f'seg-{i:03d}.ts'))
        if cur and (cur_dur + durs[i] > 45.0 or cur_size + sz > 18_000_000):
            groups.append(cur); cur, cur_dur, cur_size = [], 0.0, 0
        cur.append(i); cur_dur += durs[i]; cur_size += sz
    if cur: groups.append(cur)
    chunk_durs = []
    for gi, g in enumerate(groups):
        with open(os.path.join(outdir, f'chunk-{gi:03d}.ts'), 'wb') as out:
            for i in g:
                with open(os.path.join(outdir, f'seg-{i:03d}.ts'), 'rb') as f: out.write(f.read())
        chunk_durs.append(sum(durs[i] for i in g))
    for i in range(len(segs)):
        try: os.unlink(os.path.join(outdir, f'seg-{i:03d}.ts'))
        except OSError: pass
    target = int(max(chunk_durs)) + 1 if chunk_durs else 10
    with open(os.path.join(outdir, 'playlist.m3u8'), 'w') as f:
        f.write('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n')
        f.write(f'#EXT-X-TARGETDURATION:{target}\n#EXT-X-MEDIA-SEQUENCE:0\n')
        for i, dd in enumerate(chunk_durs): f.write(f'#EXTINF:{dd:.3f},\nchunk-{i:03d}.ts\n')
        f.write('#EXT-X-ENDLIST\n')
    log(f'  ep{num}: {len(groups)} potongan besar, {sum(chunk_durs):.0f} dtk OK')
    return True

def existing_eps():
    """Episode yang sudah terunggah di repo (untuk resume): nama dir ep<n> di <WID>/."""
    s, r = gh('GET', f'/repos/{REPO}/contents/{WID}')
    if s != 200 or not isinstance(r, list): return set()
    out = set()
    for it in r:
        nm = it.get('name', '')
        if it.get('type') == 'dir' and nm.startswith('ep') and nm[2:].isdigit(): out.add(int(nm[2:]))
    return out

def upload_episode(ep, edir, parent):
    """Unggah satu episode sebagai satu commit; kembalikan sha commit baru."""
    names = sorted(os.listdir(edir))
    def up(fn):
        data = open(os.path.join(edir, fn), 'rb').read()
        want = blob_sha(data)
        s2, b = gh('POST', f'/repos/{REPO}/git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
        return {'path': f'{WID}/ep{ep}/{fn}', 'mode': '100644', 'type': 'blob', 'sha': b.get('sha') or want}
    with ThreadPoolExecutor(max_workers=12) as ex:
        items = list(ex.map(up, names))
    s, pc = gh('GET', f'/repos/{REPO}/git/commits/{parent}'); base_tree = pc['tree']['sha']
    s, tree = gh('POST', f'/repos/{REPO}/git/trees', {'base_tree': base_tree, 'tree': items})
    if s not in (200, 201): log('tree gagal', s); sys.exit(4)
    s, commit = gh('POST', f'/repos/{REPO}/git/commits', {
        'message': f'{WID} episode {ep} (segmen HLS)', 'tree': tree['sha'], 'parents': [parent]})
    if s not in (200, 201): log('commit gagal', s); sys.exit(4)
    parent = commit['sha']
    s, upd = gh('PATCH', f'/repos/{REPO}/git/refs/heads/main', {'sha': parent})
    if s != 200: log('ref gagal', s); sys.exit(4)
    log(f'  ep{ep} commit ok ({parent[:7]})')
    return parent

def main():
    os.makedirs(OUT, exist_ok=True)
    have = existing_eps()
    if have: log(f'resume: {len(have)} episode sudah ada di repo, dilanjutkan')
    s, ref = gh('GET', f'/repos/{REPO}/git/ref/heads/main')
    parent = ref['object']['sha']
    for ep in range(1, TOTAL + 1):
        if ep in have: continue
        edir = os.path.join(OUT, f'ep{ep}')
        ok = False
        for attempt in range(6):
            if download_episode(SLUG_ID, ep, edir): ok = True; break
            log(f'  ep{ep}: unduh belum utuh (percobaan {attempt + 1}/6)'); time.sleep(10)
        if not ok:
            log(f'EP{ep} GAGAL — berhenti (episode yang sudah selesai aman tersimpan di repo)'); sys.exit(3)
        parent = upload_episode(ep, edir, parent)
    pin = parent
    manifest = {'wid': WID, 'anilistId': ANILIST, 'name': NAME, 'totalEpisodes': TOTAL,
                'repo': REPO, 'pin': pin}
    mdata = json.dumps(manifest, indent=1).encode()
    s, blob = gh('POST', f'/repos/{REPO}/git/blobs', {'content': base64.b64encode(mdata).decode(), 'encoding': 'base64'})
    s, pc = gh('GET', f'/repos/{REPO}/git/commits/{parent}'); base_tree = pc['tree']['sha']
    s, tree = gh('POST', f'/repos/{REPO}/git/trees', {'base_tree': base_tree, 'tree': [
        {'path': f'manifests/{WID}.json', 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
    s, commit = gh('POST', f'/repos/{REPO}/git/commits', {
        'message': f'manifest {WID}', 'tree': tree['sha'], 'parents': [parent]})
    s, upd = gh('PATCH', f'/repos/{REPO}/git/refs/heads/main', {'sha': commit['sha']})
    print('MANIFEST_PIN', pin, flush=True)
    log(f'SELESAI {WID}: pin {pin[:7]} — menunggu diterbitkan pengawas katalog')

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Cut one z12 cell of NAIP imagery into a PMTiles archive.

Third-party packages are imported inside the functions that use them, so an
import failure is reported as a status word and the parent never loads GDAL
before forking workers.
"""
import argparse
import logging
import multiprocessing
import os
import re
import shutil
import sys
import tempfile
import traceback
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from typing import NamedTuple

STAC_URL = 'https://planetarycomputer.microsoft.com/api/stac/v1'
COLLECTION = 'naip'
BUCKET = 'spotter-imagery'
KEY = 'naip/v1/cells/{x}_{y}.pmtiles'
CONTENT_TYPE = 'application/vnd.pmtiles'
ATTRIBUTION = 'USDA NAIP (public domain)'
R2_ENV = ('R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT')

CELL_Z, CHUNK_Z, MAX_Z = 12, 14, 17
TILE_PX = 512
CHUNK_PX = TILE_PX << (MAX_Z - CHUNK_Z)
WEBP_QUALITY, WEBP_METHOD = 80, 4
MIN_GAP_FRACTION = 1e-6
STAC_TIMEOUT_S = 60

GDAL_ENV = {
    'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR',
    'CPL_VSIL_CURL_ALLOWED_EXTENSIONS': '.tif',
    'CPL_DEBUG': 'OFF',
    'CPL_LOG': os.devnull,
    'GDAL_CACHEMAX': '512',
    'GDAL_HTTP_TIMEOUT': '60',
    'GDAL_HTTP_MAX_RETRY': '5',
    'GDAL_HTTP_RETRY_DELAY': '2',
}


class Source(NamedTuple):
    href: str
    date: str
    geometry: dict


class Fail(Exception):
    """args[0] is the status code printed after `fail:`."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail('E1', message)


@contextmanager
def stage(code):
    try:
        yield
    except Fail:
        raise
    except Exception as exc:
        raise Fail(code) from exc


def parse_cell(text):
    match = re.fullmatch(rf'{CELL_Z}/([0-9]+)/([0-9]+)', text)
    if not match:
        raise Fail('E1', 'OD1_CELL must be z/x/y at the cell zoom')
    x, y = int(match[1]), int(match[2])
    if max(x, y) >= 1 << CELL_Z:
        raise Fail('E1', 'OD1_CELL index out of range')
    return x, y


def search_sources(bbox):
    import planetary_computer
    import pystac_client

    api = pystac_client.Client.open(
        STAC_URL, modifier=planetary_computer.sign_inplace, timeout=STAC_TIMEOUT_S)
    items = api.search(collections=[COLLECTION], bbox=bbox).item_collection()
    return [Source(i.assets['image'].href, i.datetime.date().isoformat(), i.geometry)
            for i in items]


def pick_sources(sources, bbox):
    """Newest year first; older years only where they add cell coverage."""
    from shapely.geometry import box, shape
    from shapely.ops import unary_union

    gap = box(*bbox)
    min_gap = gap.area * MIN_GAP_FRACTION
    picked = []
    for year in sorted({s.date[:4] for s in sources}, reverse=True):
        group = [s for s in sources if s.date[:4] == year]
        footprint = unary_union([shape(s.geometry) for s in group])
        if gap.intersection(footprint).area > min_gap:
            picked += sorted(group, key=lambda s: (s.date, s.href), reverse=True)
            gap = gap.difference(footprint)
        if gap.area <= min_gap:
            break
    return picked


def read_chunk(x, y, sources):
    import mercantile
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT
    from shapely.geometry import box, shape

    tile = mercantile.Tile(x, y, CHUNK_Z)
    extent = box(*mercantile.bounds(tile))
    transform = from_bounds(*mercantile.xy_bounds(tile), CHUNK_PX, CHUNK_PX)
    rgb = np.zeros((3, CHUNK_PX, CHUNK_PX), np.uint8)
    valid = np.zeros((CHUNK_PX, CHUNK_PX), bool)
    for source in sources:
        if valid.all():
            break
        if not shape(source.geometry).intersects(extent):
            continue
        with rasterio.open(source.href) as src, WarpedVRT(
                src, crs='EPSG:3857', transform=transform,
                width=CHUNK_PX, height=CHUNK_PX, resampling=Resampling.cubic,
                src_nodata=0, nodata=0) as vrt:
            pixels = vrt.read((1, 2, 3))
        filled = pixels.any(0)
        fresh = filled & ~valid
        rgb[:, fresh] = pixels[:, fresh]
        valid |= filled
    return np.dstack([rgb[0], rgb[1], rgb[2], valid * np.uint8(255)])


def encode(rgba):
    import io
    import numpy as np
    from PIL import Image

    alpha = rgba[..., 3]
    if not alpha.any():
        return None
    pixels = rgba if (alpha < 255).any() else rgba[..., :3]
    out = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(pixels)).save(
        out, 'WEBP', quality=WEBP_QUALITY, method=WEBP_METHOD)
    return out.getvalue()


def cut_chunk(x, y, sources):
    """Return (tiles for CHUNK_Z..MAX_Z, the CHUNK_Z image or None if empty)."""
    import numpy as np
    from PIL import Image

    with stage('E3'):
        rgba = read_chunk(x, y, sources)
    with stage('E4'):
        if not rgba[..., 3].any():
            return [], None
        image = Image.fromarray(rgba)
        tiles = []
        for z in range(MAX_Z, CHUNK_Z - 1, -1):
            n = 1 << (z - CHUNK_Z)
            size = TILE_PX * n
            level = np.asarray(image if size == CHUNK_PX
                               else image.resize((size, size), Image.LANCZOS))
            for row in range(n):
                for col in range(n):
                    data = encode(level[row * TILE_PX:(row + 1) * TILE_PX,
                                        col * TILE_PX:(col + 1) * TILE_PX])
                    if data:
                        tiles.append((z, x * n + col, y * n + row, data))
        return tiles, level


def downsample(images):
    """Merge {(x, y): image} one zoom up with LANCZOS."""
    import numpy as np
    from PIL import Image

    canvases = {}
    for (x, y), image in images.items():
        canvas = canvases.setdefault((x // 2, y // 2),
                                     np.zeros((TILE_PX * 2, TILE_PX * 2, 4), np.uint8))
        top, left = (y % 2) * TILE_PX, (x % 2) * TILE_PX
        canvas[top:top + TILE_PX, left:left + TILE_PX] = image
    return {key: np.asarray(Image.fromarray(canvas).resize((TILE_PX, TILE_PX), Image.LANCZOS))
            for key, canvas in canvases.items()}


def render_cell(x, y, sources):
    import mercantile

    chunks = mercantile.children(mercantile.Tile(x, y, CELL_Z), zoom=CHUNK_Z)
    with stage('E4'), ProcessPoolExecutor(
            os.cpu_count(), mp_context=multiprocessing.get_context('fork')) as pool:
        futures = [(c, pool.submit(cut_chunk, c.x, c.y, sources)) for c in chunks]
        results = [(c, f.result()) for c, f in futures]
    with stage('E4'):
        tiles = [tile for _, (chunk_tiles, _) in results for tile in chunk_tiles]
        images = {(c.x, c.y): image for c, (_, image) in results if image is not None}
        for z in range(CHUNK_Z - 1, CELL_Z - 1, -1):
            images = downsample(images)
            tiles += [(z, tx, ty, data) for (tx, ty), image in images.items()
                      if (data := encode(image))]
    return tiles


def write_archive(path, tiles, x, y, date):
    import mercantile
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    west, south, east, north = mercantile.bounds(x, y, CELL_Z)
    with open(path, 'wb') as out:
        writer = Writer(out)
        for tile_id, data in sorted((zxy_to_tileid(z, tx, ty), data) for z, tx, ty, data in tiles):
            writer.write_tile(tile_id, data)
        writer.finalize({
            'tile_type': TileType.WEBP,
            'tile_compression': Compression.NONE,
            'min_zoom': CELL_Z,
            'max_zoom': MAX_Z,
            'min_lon_e7': round(west * 1e7),
            'min_lat_e7': round(south * 1e7),
            'max_lon_e7': round(east * 1e7),
            'max_lat_e7': round(north * 1e7),
            'center_zoom': CELL_Z,
            'center_lon_e7': round((west + east) / 2 * 1e7),
            'center_lat_e7': round((south + north) / 2 * 1e7),
        }, {
            'attribution': ATTRIBUTION,
            'tileSize': TILE_PX,
            'format': 'webp',
            'acquisition_date': date,
        })


def upload(path, x, y):
    import boto3
    from botocore.config import Config

    client = boto3.client(
        's3', region_name='auto', endpoint_url=os.environ['R2_ENDPOINT'],
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        config=Config(request_checksum_calculation='when_required',
                      response_checksum_validation='when_required'))
    client.upload_file(path, BUCKET, KEY.format(x=x, y=y),
                       ExtraArgs={'ContentType': CONTENT_TYPE})


def run(args):
    if (args.out is None) == args.dry_run:
        raise Fail('E1', '--dry-run and --out go together')
    x, y = parse_cell(os.environ.get('OD1_CELL', ''))
    if not args.dry_run and not all(os.environ.get(name) for name in R2_ENV):
        raise Fail('E5', 'R2 environment is incomplete')
    with stage('E2'):
        import mercantile

        bbox = tuple(mercantile.bounds(x, y, CELL_Z))
        sources = pick_sources(search_sources(bbox), bbox)
    if not sources:
        return 'none'
    tiles = render_cell(x, y, sources)
    if not tiles:
        return 'none'
    with tempfile.TemporaryDirectory() as work:
        archive = os.path.join(work, 'cell.pmtiles')
        with stage('E4'):
            write_archive(archive, tiles, x, y, max(s.date for s in sources))
            if args.dry_run:
                shutil.move(archive, args.out)
        if not args.dry_run:
            with stage('E5'):
                upload(archive, x, y)
    return 'ok'


def open_error_file(path):
    if path:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return tempfile.mkstemp(suffix='.err', dir=os.environ.get('RUNNER_TEMP') or None)[0]


def redirect_output(fd):
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)


def main():
    status_fd = os.dup(1)
    redirect_output(os.open(os.devnull, os.O_WRONLY))
    logging.disable(logging.CRITICAL)
    os.environ.update(GDAL_ENV)
    parser = Parser(add_help=False)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--out')
    parser.add_argument('--err')
    try:
        with stage('E1'):
            args = parser.parse_args()
            redirect_output(open_error_file(args.err))
        status = run(args)
    except Fail as exc:
        traceback.print_exc()
        status = f'fail:{exc.args[0]}'
    except Exception:
        traceback.print_exc()
        status = 'fail:E4'
    sys.stderr.flush()
    os.write(status_fd, f'{status}\n'.encode())
    return 1 if status.startswith('fail') else 0


if __name__ == '__main__':
    sys.exit(main())

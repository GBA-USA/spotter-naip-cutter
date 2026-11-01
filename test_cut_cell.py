import io
import json
import os
import subprocess
import sys
from pathlib import Path

import mercantile
import numpy as np
import pytest
import rasterio
from PIL import Image
from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import TileType
from rasterio.transform import from_bounds
from shapely.geometry import box, mapping

import cut_cell

REPO = Path(__file__).parent
CELL_X, CELL_Y = 100, 200
CELL = f'12/{CELL_X}/{CELL_Y}'
BBOX = tuple(mercantile.bounds(CELL_X, CELL_Y, 12))
FOOTPRINT = mapping(box(*BBOX))
COLOR = (200, 100, 50)

DRIVER = """
import json, os, sys
sys.path.insert(0, os.environ['TEST_REPO'])
import boto3, cut_cell

def search(bbox):
    os.write(1, b'noise-stdout')
    os.write(2, b'noise-stderr')
    if os.environ.get('TEST_STAC_DOWN'):
        raise RuntimeError('stac unavailable')
    return [cut_cell.Source(*s) for s in json.loads(os.environ['TEST_SOURCES'])]

class FakeS3:
    def upload_file(self, path, bucket, key, ExtraArgs):
        if os.environ.get('TEST_UPLOAD_DOWN'):
            raise RuntimeError('upload refused')
        with open(os.environ['TEST_UPLOAD_LOG'], 'w') as log:
            json.dump([bucket, key, ExtraArgs, os.path.getsize(path)], log)

cut_cell.search_sources = search
boto3.client = lambda *args, **kwargs: FakeS3()
sys.exit(cut_cell.main())
"""


def run_cut(*flags, cell=CELL, sources=(), **env):
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(('R2_', 'OD1_'))}
    full_env.update(TEST_REPO=str(REPO), TEST_SOURCES=json.dumps(sources), **env)
    if cell is not None:
        full_env['OD1_CELL'] = cell
    return subprocess.run([sys.executable, '-c', DRIVER, *flags],
                          env=full_env, capture_output=True, timeout=300)


def assert_status(result, word, code):
    assert result.stdout == f'{word}\n'.encode()
    assert result.stderr == b''
    assert result.returncode == code


@pytest.fixture
def raster(tmp_path):
    """A cell-sized raster whose left 30% is nodata."""
    size = 1024
    pixels = np.zeros((3, size, size), np.uint8)
    for band, value in enumerate(COLOR):
        pixels[band, :, int(size * 0.3):] = value
    path = tmp_path / 'scene.tif'
    with rasterio.open(path, 'w', driver='GTiff', width=size, height=size, count=3,
                       dtype='uint8', crs='EPSG:3857', nodata=0,
                       transform=from_bounds(*mercantile.xy_bounds(CELL_X, CELL_Y, 12), size, size)) as dst:
        dst.write(pixels)
    return path


def archive_tile(reader, z, x, y):
    data = reader.get(z, x, y)
    return data and Image.open(io.BytesIO(data))


def test_dry_run_writes_archive_with_transparency_and_skipped_tiles(raster, tmp_path):
    out = tmp_path / 'cell.pmtiles'
    result = run_cut('--dry-run', '--out', str(out), '--err', str(tmp_path / 'err'),
                     sources=[[str(raster), '2021-06-01', FOOTPRINT]])
    assert_status(result, 'ok', 0)

    with open(out, 'rb') as f:
        reader = Reader(MmapSource(f))
        header = reader.header()
        assert header['tile_type'] == TileType.WEBP
        assert (header['min_zoom'], header['max_zoom']) == (12, 17)
        assert reader.metadata() == {'attribution': 'USDA NAIP (public domain)', 'tileSize': 512,
                                     'format': 'webp', 'acquisition_date': '2021-06-01'}
        assert header['min_lon_e7'] == round(BBOX[0] * 1e7)

        top = archive_tile(reader, 12, CELL_X, CELL_Y)
        assert top.size == (512, 512) and top.mode == 'RGBA'

        n = 1 << 5
        solid = archive_tile(reader, 17, CELL_X * n + n - 1, CELL_Y * n)
        assert solid.mode == 'RGB'
        assert np.abs(np.asarray(solid, int) - COLOR).max() <= 3

        assert reader.get(17, CELL_X * n, CELL_Y * n) is None
        assert archive_tile(reader, 17, CELL_X * n + int(n * 0.3), CELL_Y * n).mode == 'RGBA'


def test_redirected_output_lands_in_error_file_only(tmp_path):
    err = tmp_path / 'err'
    assert_status(run_cut('--dry-run', '--out', str(tmp_path / 'x'), '--err', str(err)), 'none', 0)
    assert err.read_bytes() == b'noise-stdoutnoise-stderr'


def test_default_error_file_is_a_random_name_in_runner_temp(tmp_path):
    assert_status(run_cut('--dry-run', '--out', str(tmp_path / 'x'), RUNNER_TEMP=str(tmp_path)), 'none', 0)
    [err] = tmp_path.glob('*.err')
    assert err.read_bytes() == b'noise-stdoutnoise-stderr'


def test_no_coverage_reports_none_and_writes_nothing(tmp_path):
    out = tmp_path / 'cell.pmtiles'
    assert_status(run_cut('--dry-run', '--out', str(out), '--err', str(tmp_path / 'err')), 'none', 0)
    assert not out.exists()


@pytest.mark.parametrize('cell', [None, '', '11/1/1', '12/4096/0', '12/1/1\n', '12/1', 'x'])
def test_bad_cell_is_e1(cell, tmp_path):
    assert_status(run_cut('--dry-run', '--out', str(tmp_path / 'x'), '--err', str(tmp_path / 'err'),
                          cell=cell), 'fail:E1', 1)


@pytest.mark.parametrize('flags', [['--bogus'], ['--dry-run'], ['--out', 'x'], ['--help']])
def test_bad_flags_are_e1(flags):
    assert_status(run_cut(*flags), 'fail:E1', 1)


def test_stac_failure_is_e2_with_traceback_in_error_file(tmp_path):
    err = tmp_path / 'err'
    result = run_cut('--dry-run', '--out', str(tmp_path / 'x'), '--err', str(err), TEST_STAC_DOWN='1')
    assert_status(result, 'fail:E2', 1)
    assert 'stac unavailable' in err.read_text()


def test_truncated_source_is_e3_and_leaves_no_partial_archive(raster, tmp_path):
    os.truncate(raster, raster.stat().st_size // 2)
    out = tmp_path / 'cell.pmtiles'
    result = run_cut('--dry-run', '--out', str(out), '--err', str(tmp_path / 'err'),
                     sources=[[str(raster), '2021-06-01', FOOTPRINT]])
    assert_status(result, 'fail:E3', 1)
    assert not out.exists()


R2 = {'R2_ACCESS_KEY_ID': 'id', 'R2_SECRET_ACCESS_KEY': 'secret', 'R2_ENDPOINT': 'http://r2.invalid'}


def test_upload_uses_cell_key_and_content_type(raster, tmp_path):
    log = tmp_path / 'upload.json'
    result = run_cut('--err', str(tmp_path / 'err'), sources=[[str(raster), '2021-06-01', FOOTPRINT]],
                     TEST_UPLOAD_LOG=str(log), **R2)
    assert_status(result, 'ok', 0)
    bucket, key, extra, size = json.loads(log.read_text())
    assert (bucket, key) == ('spotter-imagery', f'naip/v1/cells/{CELL_X}_{CELL_Y}.pmtiles')
    assert extra == {'ContentType': 'application/vnd.pmtiles'}
    assert size > 0


def test_upload_failure_is_e5(raster, tmp_path):
    result = run_cut('--err', str(tmp_path / 'err'), sources=[[str(raster), '2021-06-01', FOOTPRINT]],
                     TEST_UPLOAD_DOWN='1', **R2)
    assert_status(result, 'fail:E5', 1)


def test_missing_r2_env_is_e5_before_any_search(tmp_path):
    err = tmp_path / 'err'
    assert_status(run_cut('--err', str(err)), 'fail:E5', 1)
    assert b'noise' not in err.read_bytes()


def fake_source(date, bounds):
    return cut_cell.Source(date, date, mapping(box(*bounds)))


def years(sources):
    return [s.date[:4] for s in sources]


def test_newest_full_year_hides_older_years():
    picked = cut_cell.pick_sources([fake_source('2019-01-01', (0, 0, 1, 1)),
                                    fake_source('2022-01-01', (0, 0, 1, 1))], (0, 0, 1, 1))
    assert years(picked) == ['2022']


def test_gap_in_newest_year_is_filled_from_next_newest_only():
    picked = cut_cell.pick_sources([fake_source('2016-01-01', (0, 0, 1, 1)),
                                    fake_source('2019-01-01', (0, 0, 1, 1)),
                                    fake_source('2022-01-01', (0, 0, 0.5, 1))], (0, 0, 1, 1))
    assert years(picked) == ['2022', '2019']


def test_item_touching_only_the_cell_edge_is_ignored():
    picked = cut_cell.pick_sources([fake_source('2022-01-01', (1, 0, 2, 1))], (0, 0, 1, 1))
    assert picked == []
